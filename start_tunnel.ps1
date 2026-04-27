param(
  [int]$Port = 8000
)

Write-Host "Start the FastAPI server in another terminal first:"
Write-Host "python -m uvicorn backend.app:app --host 0.0.0.0 --port $Port --reload"
Write-Host ""
Write-Host "Starting Cloudflared quick tunnel..."
cloudflared tunnel --url "http://localhost:$Port"
