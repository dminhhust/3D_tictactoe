param(
  [int]$Port = 8000
)

python -m uvicorn backend.app:app --host 0.0.0.0 --port $Port --reload
