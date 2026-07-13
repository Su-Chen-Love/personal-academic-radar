"""Compatibility shim for older pip versions that lack PEP 660 support."""

from setuptools import find_packages, setup


setup(
    name="personal-academic-radar",
    version="0.4.0",
    description="A local-first personal academic monitoring and recommendation system",
    package_dir={"": "src"},
    packages=find_packages("src"),
    package_data={"academic_radar": ["migrations/*.sql", "templates/*.html", "static/*.css"]},
    include_package_data=True,
    python_requires=">=3.9",
    install_requires=["fastapi>=0.115,<1", "jinja2>=3.1,<4", "tomli>=2; python_version<'3.11'", "uvicorn>=0.30,<1"],
    entry_points={"console_scripts": ["academic-radar=academic_radar.cli:main"]},
)
