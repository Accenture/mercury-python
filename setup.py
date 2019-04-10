from distutils.core import setup

setup(name='mercury',
      version='1.11.33',
      description='Python Language pack for Mercury',
      author='Eric Law',
      author_email='eric.law@accenture.com',
      url='http://github.accenture.com/Mercury',
      packages=['mercury', 'mercury.system', 'mercury.resources'],
      install_requires=[
            'aiohttp', 'msgpack-python'
      ]
     )
