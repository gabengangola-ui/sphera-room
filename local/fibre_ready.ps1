# SPHERA Fibre Ready — runs when new internet is connected
# Updates DuckDNS, verifies connectivity, starts everything

Write-Host "⬡ SPHERA Fibre Setup" -ForegroundColor Cyan
Write-Host ""

# Step 1: Get new public IP
$newIP = (Invoke-WebRequest -Uri "https://api.ipify.org" -UseBasicParsing).Content
Write-Host "Public IP: $newIP"

# Step 2: Update DuckDNS
$token = "35002ec5-ef17-4a57-b322-8e9eff056fc5"
$result = (Invoke-WebRequest -Uri "https://www.duckdns.org/update?domains=sphera-room&token=$token&ip=$newIP" -UseBasicParsing).Content
Write-Host "DuckDNS update: $result"

# Step 3: Start server
Write-Host ""
Write-Host "Starting SPHERA server..." -ForegroundColor Green
$env:CLAUDE_KEY="ck-sphera"
$env:SOBA_KEY="sk-sphera"  
$env:ARCIDES_KEY="ak-sphera"
$env:BRIDGE_KEY="br-sphera"
$env:SPHERA_DB="C:\Users\lione\sphera-room\sphera.db"

Start-Process powershell -ArgumentList "-Command cd C:\Users\lione\sphera-room; python3 local/server.py" -WindowStyle Normal

Start-Sleep -Seconds 3

# Step 4: Test local
$local = (Invoke-WebRequest -Uri "http://localhost:8765/health" -Headers @{"Authorization"="Bearer ck-sphera"} -UseBasicParsing).Content
Write-Host "Local server: $local"

# Step 5: Test via DuckDNS (may take 60s for DNS to propagate)
Write-Host ""
Write-Host "Testing DuckDNS connectivity..." -ForegroundColor Yellow
Start-Sleep -Seconds 5
try {
    $remote = (Invoke-WebRequest -Uri "http://sphera-room.duckdns.org:8765/health" -Headers @{"Authorization"="Bearer ck-sphera"} -UseBasicParsing -TimeoutSec 10).Content
    Write-Host "Remote access: $remote" -ForegroundColor Green
    Write-Host ""
    Write-Host "✓ SPHERA IS LIVE ON YOUR HARDWARE" -ForegroundColor Green
    Write-Host "  URL: http://sphera-room.duckdns.org:8765" -ForegroundColor Cyan
} catch {
    Write-Host "Remote not reachable yet — check router port forwarding (port 8765 → 192.168.1.185)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Start bridge in another window:"
Write-Host '$env:GMAIL_APP_PASS="YOUR_APP_PASSWORD"; python3 local/bridge_daemon.py'
