$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$envFile = Join-Path $PSScriptRoot ".env.local"
if (Test-Path -LiteralPath $envFile) {
  Get-Content -LiteralPath $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
      $parts = $line.Split([char]"=", 2)
      $name = $parts[0].Trim()
      $value = $parts[1].Trim().Trim('"').Trim("'")
      if ($name) {
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
      }
    }
  }
}

python backend/server.py
