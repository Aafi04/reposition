Clear-Host
if (-not $env:GEMINI_API_KEY -and $env:GOOGLE_API_KEY) {
    $env:GEMINI_API_KEY = $env:GOOGLE_API_KEY
}
Set-Location "C:\Users\Aafi\Desktop\Project"
& .\.venv\Scripts\python.exe main.py run https://github.com/Aafi04/onlinevotingsystem
