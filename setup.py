from setuptools import setup, find_packages


setup(
    name='fc.qemu',
    version='0.4.2',
    author='Christian Kauhaus, Christian Theune',
    author_email='mail@gocept.com',
    url='http://bitbucket.org/flyingcircus/fc.qemu',
    description="""\
QEMU VM management utilities""",
    packages=find_packages('src'),
    package_dir={'': 'src'},
    include_package_data=True,
    classifiers=[
        'Development Status :: 4 - Beta',
        'Environment :: Console',
        'Programming Language :: Python :: 2.7',
    ],
    zip_safe=False,
    license='BSD',
    namespace_packages=['fc'],
    install_requires=[
        'setuptools',
        'mock',
        'PyYaml',
        'psutil',
    ],
    entry_points={
        'console_scripts': [
            'fc-qemu = fc.qemu.main:main',
        ],
    },
)
