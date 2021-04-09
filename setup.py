from setuptools import find_packages, setup

with open('README.txt') as f:
    readme = f.read() + '\n'
with open('CHANGES.txt') as f:
    readme += f.read() + '\n'
with open('HACKING.txt') as f:
    readme += f.read()

setup(
    name='fc.qemu',
    version='1.1.5',
    author='Christian Kauhaus, Christian Theune',
    author_email='mail@flyingcircus.io',
    url='http://github.com/flyingcircusio/fc.qemu',
    description='Qemu VM management utilities',
    long_description=readme,
    packages=find_packages('src'),
    package_dir={'': 'src'},
    include_package_data=True,
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Environment :: Console',
        'Programming Language :: Python :: 2.7',
    ],
    zip_safe=False,
    license='BSD',
    namespace_packages=['fc'],
    install_requires=[
        'colorama==0.3.3',
        'consulate==0.6.0',
        'psutil==5.4.2',
        'PyYaml==5.3.1',
        'requests==2.11.1',
        'setuptools',
        'structlog==16.1.0',
    ],
    entry_points={
        'console_scripts': [
            'fc-qemu = fc.qemu.main:main',
        ],
    },
)
