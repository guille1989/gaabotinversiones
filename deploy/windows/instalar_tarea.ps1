<#
.SYNOPSIS
    Registra en el Programador de tareas de Windows el bot (y una copia de seguridad diaria).

.DESCRIPTION
    Ejecutar en una PowerShell abierta COMO ADMINISTRADOR, desde la carpeta del proyecto:

        powershell -ExecutionPolicy Bypass -File deploy\windows\instalar_tarea.ps1

    Crea dos tareas:
      GaaInversiones-<modo>   arranca el bot al encender el PC (sin necesidad de iniciar
                              sesion) y al iniciar sesion; si el proceso muere, Windows lo
                              relanza cada minuto. No guarda tu contrasena (inicio S4U).
      GaaInversiones-copia    copia de seguridad diaria a las 03:30 (gaa copia-seguridad).

    Para el modo real: ejecuta antes una vez `gaa real` a mano y escribe la confirmacion;
    la tarea reutiliza esa autorizacion mientras no cambie la configuracion.

    Para quitarlo todo: desinstalar_tarea.ps1
#>
param(
    [ValidateSet("paper", "real")]
    [string]$Modo = "paper",
    [string]$HoraCopia = "03:30"
)

$ErrorActionPreference = "Stop"
$Raiz = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Vigilante = Join-Path $PSScriptRoot "iniciar_bot.ps1"
$Gaa = Join-Path $Raiz ".venv\Scripts\gaa.exe"
$Usuario = "$env:USERDOMAIN\$env:USERNAME"

$esAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $esAdmin) {
    Write-Error "Abre PowerShell como administrador y vuelve a ejecutar este script."
}
if (-not (Test-Path $Gaa)) { Write-Error "No existe $Gaa : instala el proyecto antes." }

$principal = New-ScheduledTaskPrincipal -UserId $Usuario -LogonType S4U -RunLevel Limited

# --- Bot ---------------------------------------------------------------------------------
$accionBot = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $Raiz `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Vigilante`" -Modo $Modo"
$disparadores = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn -User $Usuario)
)
$ajustesBot = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "GaaInversiones-$Modo" -Action $accionBot -Trigger $disparadores `
    -Principal $principal -Settings $ajustesBot -Force `
    -Description "Bot de GaaInversiones en modo $Modo (vigilante con reinicio automatico)" | Out-Null

# --- Copia de seguridad diaria ----------------------------------------------------------
$accionCopia = New-ScheduledTaskAction -Execute $Gaa -Argument "copia-seguridad" -WorkingDirectory $Raiz
$diaria = New-ScheduledTaskTrigger -Daily -At $HoraCopia
$ajustesCopia = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "GaaInversiones-copia" -Action $accionCopia -Trigger $diaria `
    -Principal $principal -Settings $ajustesCopia -Force `
    -Description "Copia de seguridad diaria del estado y el registro de GaaInversiones" | Out-Null

Write-Host "Tareas creadas: GaaInversiones-$Modo y GaaInversiones-copia."
Write-Host "Arrancar ya:  Start-ScheduledTask -TaskName GaaInversiones-$Modo"
Write-Host "Ver estado:   .venv\Scripts\gaa.exe estado   (y el log en logs\servicio.log)"
