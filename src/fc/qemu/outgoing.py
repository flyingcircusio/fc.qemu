import pprint
import random
import threading
import time
import xmlrpc.client

from .exc import ConfigChanged
from .timeout import TimeOut


class Heartbeat(object):
    """Continuously ping a target in the background to indicate that
    we're still here.

    Run this in a separate thread so that blocks from other things (like
    the Qemu monitor) do not cause the remote side to time out and thus
    block any chance for recovery.

    """

    cookie = None
    failed = False

    def __init__(self, log):
        self.thread = threading.Thread(target=self.run)
        self.running = False
        self.url = None
        self.log = log
        self.log.debug("heartbeat-initialized")

    def start(self):
        assert self.url is not None
        if self.running:
            return
        self.connection = xmlrpc.client.ServerProxy(self.url, allow_none=True)
        self.running = True
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.running = False

    def run(self):
        self.log.debug("started-heartbeat-ping")
        try:
            while self.running:
                try:
                    self.log.debug("heartbeat-ping")
                    self.connection.ping(self.cookie)
                except Exception:
                    self.log.exception("ping-failed", exc_info=True)
                    self.failed = True
                time.sleep(10)
        finally:
            self.log.debug("stopped-heartbeat-ping")

    def propagate(self):
        if self.failed:
            self.log.warning("heartbeat-propagate")
            raise RuntimeError("Heartbeat failed.")


class Outgoing(object):
    migration_exitcode = None
    target = None
    cookie = None

    # How long to wait until we discover an inmigrate service?
    # This should happen relatively fast, but just in case we'll keep a high
    # timeout. The agents should be spawned relatively quickly everywhere.
    connect_timeout = 60 * 60  # 1 hour

    # How long to wait until we get a migration lock?
    # This can happen quite slowly as during busy times migration may be slow
    # and some large and busy VMs can take 1-2hours (on Gigabit). If this adds
    # up over multiple hosts I'm giving a grace period of up to 12 hours here.
    migration_lock_timeout = 12 * 60 * 60  # 12 hours.

    def __init__(self, agent):
        self.agent = agent
        self.log = agent.log
        self.name = agent.name
        self.consul = agent.consul
        self.heartbeat = Heartbeat(self.log)

    def __call__(self):
        self.cookie = self.agent.ceph.auth_cookie()
        self.log.debug("setup-outgoing-migration", cookie=self.cookie)
        with self:
            self.connect()
            self.acquire_migration_locks()
            self.transfer_ceph_locks()
            self.migrate()

        return self.migration_exitcode

    # The context manager is suitable to express "I am going to
    # interact with this VM: please prepare connections to
    # the according backends and make sure to clean up after me.""
    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        if _exc_type is None:
            # We're on our happy path. Release the migration lock
            # optimistically and only in this case. Otherwise the
            # error reporting for why the migration has failed will
            # be screwed. And the negative path will exit the process
            # and eventually give up locks anyway.
            try:
                self.agent.qemu.release_migration_lock()
            except Exception:
                pass
            try:
                self.target.release_migration_lock(self.cookie)
            except Exception:
                pass

            self.migration_exitcode = 0

        else:
            self.migration_exitcode = 1
            try:
                # XXX silence timeout errors here to avoid unnecessary
                # traceback output
                self.log.exception(
                    "migration-failed", action="rescue", exc_info=True
                )
                self.rescue()
                return True  # swallow exception
            except:  # noqa
                # Purposeful bare except: try really hard to kill
                # our VM.
                self.log.exception(
                    "rescue-failed", exc_info=True, action="destroy"
                )
                self.destroy()

    def locate_inmigrate_service(self):
        service_name = "vm-inmigrate-" + self.name
        timeout = TimeOut(
            self.connect_timeout,
            interval=3,
            raise_on_timeout=True,
            log=self.log,
        )
        self.log.info("locate-inmigration-service")
        while timeout.tick():
            if self.agent.has_new_config():
                raise ConfigChanged()
            candidates = self.consul.catalog.service(service_name)
            if len(candidates) > 1:
                self.log.warning(
                    "multiple-services-found",
                    action="trying newest first",
                    service=service_name,
                )
            candidates = sorted(
                candidates, key=lambda i: i["ModifyIndex"], reverse=True
            )
            for candidate in candidates:
                url = "http://{}:{}".format(
                    candidate["ServiceAddress"], candidate["ServicePort"]
                )
                self.log.info("located-inmigration-service", url=url)
                try:
                    target = xmlrpc.client.ServerProxy(url, allow_none=True)
                    target.ping(self.cookie)
                except Exception as e:
                    self.log.info("connect", result="failed", reason=str(e))
                    # Not a viable service. Delete it.
                    # XXX This seems broken in consulate 0.6, waiting
                    # for the 1.0 release ...
                    # try:
                    #     self.consul.catalog.deregister(
                    #        node=candidate['Node'],
                    #        datacenter=candidate['Datacenter'],
                    #        service_id=candidate['ServiceID'])
                    #     self.log.debug('delete-stale-incoming-service', result='success')
                    #     pass
                    # except Exception:
                    #     self.log.exception('delete-stale-incoming-service', result='failed', exc_info=True)
                    pass
                else:
                    break
            else:
                # We did not find a viable target - continue waiting
                continue
            return target, url
        raise RuntimeError("failed to locate inmigrate service", service_name)

    def connect(self):
        connection, url = self.locate_inmigrate_service()
        self.target = connection
        self.heartbeat.url = url
        self.heartbeat.cookie = self.cookie
        self.heartbeat.start()

    def acquire_migration_locks(self):
        tries = 0
        self.log.info("acquire-migration-locks")
        timeout = TimeOut(
            self.migration_lock_timeout,
            interval=3,
            raise_on_timeout=True,
            log=self.log,
        )
        while timeout.tick():
            # In case that there are multiple processes waiting, randomize to
            # avoid steplock retries. We us CSMA/CD-based exponential backoff,
            # timeslot 10ms but with a max of 13 instead of 16.
            # This means we'll wait up to 20s, 10s on average if everything
            # becomes really busy and we may experience lock contention.
            tries = min([tries + 1, 11])
            timeout.interval = random.randint(1, 2**tries) * 0.01
            if self.agent.has_new_config():
                self.target.cancel(self.cookie)
                raise ConfigChanged()
            self.heartbeat.propagate()

            # Try to acquire local lock
            if self.agent.qemu.acquire_migration_lock():
                self.log.debug("acquire-local-migration-lock", result="success")
            else:
                self.log.debug("acquire-local-migration-lock", result="failure")
                continue

            # Try to acquire remote lock
            try:
                self.log.debug("acquire-remote-migration-lock")
                # We got our lock, now ask the remote side:
                if not self.target.acquire_migration_lock(self.cookie):
                    self.log.debug(
                        "acquire-remote-migration-lock", result="failure"
                    )
                    self.agent.qemu.release_migration_lock()
                    continue
                self.log.debug(
                    "acquire-remote-migration-lock", result="success"
                )
                self.heartbeat.propagate()
            except Exception:
                self.log.exception(
                    "acquire-remote-migration-lock",
                    result="failure",
                    exc_info=True,
                )
                self.agent.qemu.release_migration_lock()
                continue
            break

    def transfer_ceph_locks(self):
        self.agent.ceph.unlock()
        self.target.acquire_ceph_locks(self.cookie)

    def migrate(self):
        """Actually move VM between hosts."""
        args, config = self.agent.qemu.get_running_config()
        self.log.info("prepare-remote-environment")
        migration_address = self.target.prepare_incoming(
            self.cookie, args, config
        )
        self.log.info("start-migration", target=migration_address)
        self.agent.qemu.migrate(migration_address)
        try:
            for stat in self.agent.qemu.poll_migration_status():
                remaining = stat["ram"]["remaining"] if "ram" in stat else 0
                mbps = stat["ram"]["mbps"] if "ram" in stat else "-"
                self.log.info(
                    "migration-status",
                    status=stat["status"],
                    remaining="{0:,d}".format(remaining),
                    mbps=mbps,
                    output=pprint.pformat(stat),
                )
                self.heartbeat.propagate()
        except Exception:
            self.log.exception("error-waiting-for-migration", exc_info=True)
            raise

        status = self.agent.qemu.qmp.command("query-status")
        assert not status["running"], status
        assert status["status"] == "postmigrate", status
        self.log.info("finish-migration")
        self.destroy(kill_supervisor=True)
        try:
            self.log.info("finish-remote")
            self.target.finish_incoming(self.cookie)
        except Exception:
            self.log.exception("error-finish-remote", exc_info=True)
        self.heartbeat.stop()

    def rescue(self):
        """Outgoing rescue: try to rescue the remote side first."""
        if self.target is not None:
            try:
                self.log.info("rescue-remote")
                self.target.rescue(self.cookie)
                self.target.finish_incoming(self.cookie)
                self.log.info("rescue-remote-success", action="destroy local")
                self.destroy(kill_supervisor=True)
                # We managed to rescue on the remote side - hooray!
                self.migration_exitcode = 0
                return
            except Exception:
                self.log.exception(
                    "rescue-remote-failed",
                    exc_info=True,
                    action="destroy remote",
                )
                try:
                    self.heartbeat.stop()
                    self.target.destroy(self.cookie)
                except Exception:
                    self.log.exception("destroy-remote-failed", exc_info=True)
        try:
            self.log.info("continue-locally")
            self.agent.ceph.lock()
            assert self.agent.qemu.is_running()
        except Exception:
            self.log.exception(
                "continue-locally",
                exc_info=True,
                result="failed",
                action="destroy local",
            )
            self.destroy()
        else:
            self.log.info("continue-locally", result="success")

    def destroy(self, kill_supervisor=False):
        self.agent.qemu.destroy(kill_supervisor)
        self.agent.qemu.clean_run_files()
