#!/usr/bin/env python
# -*- coding: utf-8 -*-

from setuptools import setup

setup(name='mercury',
      version='2.5.0',
      description='Python Language pack for Mercury',
      author='Eric Law',
      author_email='eric.law@accenture.com',
      url='https://github.com/Accenture/mercury-python',
      project_urls={
            'Parent': 'https://github.com/Accenture/mercury'
      },
      packages=['mercury', 'mercury.system', 'mercury.resources'],
      package_dir={'mercury': 'mercury'},
      package_data={'mercury': ['resources/application.yml']},
      license='Apache 2.0',
      python_requires='>=3.8.0',
      install_requires=['aiohttp', 'msgpack-python', 'PyYAML', 'pytest'],
      classifiers=[
          'Programming Language :: Python :: 3',
          'Programming Language :: Python :: 3.8',
          'Programming Language :: Python :: 3.9',
          'Programming Language :: Python :: 3.10',
          'Intended Audience :: Developers',
          'License :: OSI Approved :: Apache Software License'
      ])
