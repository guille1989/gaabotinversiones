@echo off
rem Lanzador de GaaInversiones: permite escribir ".\gaa estado" en la carpeta del proyecto
rem sin activar el entorno virtual.
"%~dp0.venv\Scripts\gaa.exe" %*
