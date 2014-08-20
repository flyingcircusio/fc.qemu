from setuptools import setup, find_packages
from setuptools.command.test import test as TestCommand
import os.path as p
import sys


class PyTest(TestCommand):
    user_options = [('pytest-args=', 'a', "Arguments to pass to py.test")]

    def initialize_options(self):
        TestCommand.initialize_options(self)
        self.pytest_args = None

    def finalize_options(self):
        TestCommand.finalize_options(self)
        self.test_args = []
        self.test_suite = True

    def run_tests(self):
        #put stubs for rados and rbd bindings in (testing only)
        sys.path.append(p.join(p.dirname(p.abspath(__file__)), 'fake_libs'))
        #import here, cause outside the eggs aren't loaded
        import pytest
        pytest_args = self.pytest_args if self.pytest_args else ''
        errno = pytest.main(pytest_args + ' --pyargs fc.livemig')
        sys.exit(errno)


setup(
    name='fc.livemig',
    version='0.2.5.dev0',
    author='Christian Kauhaus',
    author_email='kc@gocept.com',
    url='http://bitbucket.org/flyingcircus/fc.livemig',
    description="""\
Qemu live migration helpers""",
    packages=find_packages('src'),
    package_dir={'': 'src'},
    include_package_data=True,
    zip_safe=False,
    license='BSD',
    namespace_packages=['fc'],
    install_requires=[
        'setuptools',
    ],
    entry_points={
        'console_scripts': [
            'fc-livemig = fc.livemig:main',
        ],
    },
    tests_require=['pytest'],
    cmdclass={'test': PyTest},
)
