@echo off
rem ==========================================================================
rem  Wall Street Skateshop Shopify Tools - demarrage en local
rem  Double-cliquez sur ce fichier : il se place dans le dossier du projet,
rem  prepare l'environnement Python si besoin, puis ouvre l'application
rem  dans le navigateur (http://localhost:8501). Fermez la fenetre pour arreter.
rem ==========================================================================
chcp 65001 >nul
title Wall Street Skateshop Shopify Tools
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERREUR] Python est introuvable. Installez-le depuis https://www.python.org
    echo          en cochant "Add python.exe to PATH", puis relancez ce fichier.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Premiere utilisation : creation de l'environnement Python...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERREUR] Impossible de creer l'environnement Python.
        pause
        exit /b 1
    )
)

rem Installe les dependances la premiere fois, puis seulement si requirements.txt a change
fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul
if errorlevel 1 (
    echo Installation des dependances, cela peut prendre quelques minutes...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 (
        echo [ERREUR] L'installation des dependances a echoue : verifiez la connexion internet.
        pause
        exit /b 1
    )
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

echo.
echo Demarrage de l'application : elle va s'ouvrir dans votre navigateur.
echo Pour l'arreter, fermez cette fenetre (ou Ctrl+C).
echo.
".venv\Scripts\python.exe" -m streamlit run app.py --browser.gatherUsageStats false
if errorlevel 1 pause
