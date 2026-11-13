from setuptools import setup, Extension
from Cython.Build import cythonize

setup(
    ext_modules=cythonize(
        Extension("data_service.configs.secure_env", ["data_service/configs/secure_env.pyx"]),
        force=True
    )
)
