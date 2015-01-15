from setuptools import setup, find_packages

with open('README.txt') as f:
    readme = f.read() + '\n'
with open('CHANGES.txt') as f:
    readme += f.read() + '\n'
with open('HACKING.txt') as f:
    readme += f.read()

setup(
    name='fc.qemu',
    version='0.6',
    author='Christian Kauhaus, Christian Theune',
    author_email='mail@flyingcircus.io',
    url='http://bitbucket.org/flyingcircus/fc.qemu',
    description='Qemu VM management utilities',
    long_description=readme,
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
