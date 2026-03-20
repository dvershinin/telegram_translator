#!/usr/bin/env python3
"""
Setup script for Telegram Translator Bot
"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

with open("requirements.txt", "r", encoding="utf-8") as fh:
    requirements = [line.strip() for line in fh if line.strip() and not line.startswith("#")]

setup(
    name="telegram-translator",
    version="1.0.0",
    author="Telegram Translator Team",
    author_email="",
    description="A Telegram bot that translates messages from monitored channels",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/telegram_translator",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.11",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "telegram-translator=src.cli:cli",
        ],
    },
    include_package_data=True,
    package_data={
        "": ["*.yml", "*.yml.example"],
    },
) 