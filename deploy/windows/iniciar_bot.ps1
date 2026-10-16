<#
.SYNOPSIS
    Vigilante del bot de GaaInversiones: lo arranca y, si se cae, lo vuelve a arrancar.

.DESCRIPTION
    Pensado para ejecutarse desde la tarea programada de Windows (ver instalar_tarea.ps1),
    aunque tambien se puede lanzar a mano. Segun el codigo con el que termina `gaa`:
      0  parada a peticion (Ctrl+C o kill switch)      -> no reinicia
      2  no puede arrancar (detenido, estado raro...)  -> no reinicia (hace falta una persona)
      1, 3 u otro (error inesperado, sin internet...)  -> espera y reinicia
    Todo queda en logs\servicio.log (el detalle del bot, en logs\gaainversiones.log).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\iniciar_bot.ps1 -Modo paper
#>
param(
    [ValidateSet("paper", "real")]
    [string]$Modo = "paper",
    [int]$EsperaReinicioS = 60,
    [switch]$UnaVez
)

$ErrorActionPreference = "Continue"
$Raiz = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $Raiz
$Gaa = Join-Path $Raiz ".venv\Scripts\gaa.exe"
$Log = Join-Path $Raiz "logs\servicio.log"
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null
$env:PYTHONIOENCODING = "utf-8"

function Escribir([string]$Texto) {
    $marca = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss")
    Add-Content -Path $Log -Value "${marca}Z [vigilante] $Texto" -Encoding UTF8
}

if (-not (Test-Path $Gaa)) {
    Escribir "No existe $Gaa : instala el proyecto (pip install -e .) antes de arrancar."
    exit 1
}

$argumentos = @($Modo)
if ($UnaVez -and $Modo -eq "paper") { $argumentos += "--una-vez" }

while ($true) {
    Escribir "Arrancando: gaa $($argumentos -join ' ')"
    & $Gaa @argumentos 2>&1 | ForEach-Object { "$_" } | Add-Content -Path $Log -Encoding UTF8
    $codigo = $LASTEXITCODE
    Escribir "gaa $Modo termino con codigo $codigo"
    if ($UnaVez) { exit $codigo }
    if ($codigo -eq 0) {
        Escribir "Parada a peticion: no se reinicia."
        exit 0
    }
    if ($codigo -eq 2) {
        Escribir "El bot no puede arrancar (revisa 'gaa estado' y el log): no se reinicia."
        exit 2
    }
    Escribir "Reinicio en $EsperaReinicioS s."
    Start-Sleep -Seconds $EsperaReinicioS
}
