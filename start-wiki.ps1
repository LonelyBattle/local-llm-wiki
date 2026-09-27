$ErrorActionPreference = 'Stop'
$env:WIKI_DATA_DIR = 'D:\LocalLLMWiki\data'
Set-Location -LiteralPath $PSScriptRoot
Start-Process 'http://127.0.0.1:3210'
node server.mjs
