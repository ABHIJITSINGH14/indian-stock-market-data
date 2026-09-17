from setuptools import setup, find_packages

with open('README.md', 'r', encoding='utf-8') as fh:
    long_description = fh.read()

setup(
    name='indian-stock-market-data',
    version='1.0.0',
    author='ABHIJITSINGH14',
    description='Automated tool for downloading and analyzing Indian stock market data',
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='https://github.com/ABHIJITSINGH14/indian-stock-market-data',
    packages=find_packages(),
    classifiers=[
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Development Status :: 4 - Beta',
        'Intended Audience :: Financial and Insurance Industry',
        'Topic :: Office/Business :: Financial :: Investment',
    ],
    python_requires='>=3.8',
    install_requires=[
        'yfinance>=0.2.32',
        'pandas>=2.0.0',
        'numpy>=1.24.0',
        'requests>=2.31.0',
        'urllib3>=1.26.18,<2',
        'beautifulsoup4>=4.12.0',
        'lxml>=4.9.0',
        'python-dateutil>=2.8.0',
        'pytz>=2023.3',
        'sqlalchemy>=2.0.0',
    ],
    entry_points={
        'console_scripts': [
            'stock-market-data=scripts.run_all:main',
            'stock-market-disclosures=scripts.collect_disclosures:main',
            'stock-screener=scripts.screen:main',
        ],
    },
)
