param(
  [string]$Config = "config.json"
)

while ($true) {
  python -m self_improver run --config $Config
  Start-Sleep -Seconds 5
}
