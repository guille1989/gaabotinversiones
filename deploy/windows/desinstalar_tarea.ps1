<#
.SYNOPSIS
    Quita las tareas programadas de GaaInversiones (como administrador).

.DESCRIPTION
    Detener la tarea mata el proceso sin despedida: el estado no se pierde (cada decision
    se guarda en una transaccion) y la posicion conserva su stop. Para una parada ordenada,
    usa antes `gaa parar` y espera a que el bot termine.
#>
$ErrorActionPreference = "Continue"
foreach ($nombre in @("GaaInversiones-paper", "GaaInversiones-real", "GaaInversiones-copia")) {
    if (Get-ScheduledTask -TaskName $nombre -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $nombre -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $nombre -Confirm:$false
        Write-Host "Quitada: $nombre"
    }
}
