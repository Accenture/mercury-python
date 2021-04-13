#!/usr/bin/env python
# -*- coding: utf-8 -*-

from setuptools import setup

setup(name='mercury',
      version='1.13.0',
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
      python_requires='>=3.6.7',
      install_requires=['aiohttp', 'msgpack-python', 'PyYAML', 'pytest'],
      classifiers=[
          'Programming Language :: Python :: 3',
          'Programming Language :: Python :: 3.5',
          'Programming Language :: Python :: 3.6',
          'Programming Language :: Python :: 3.7',
          'Intended Audience :: Developers',
          'License :: OSI Approved :: Apache Software License'
      ])
