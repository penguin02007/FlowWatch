$ErrorActionPreference = 'Stop'

$esUrl = 'http://localhost:9200/_cluster/health?pretty'
$kibanaUrl = 'http://localhost:5601/api/status'

function Get-JsonFromUrl {
    param(
        [string]$Url
    )

    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 20
        return @{ StatusCode = $response.StatusCode; Body = $response.Content }
    }
    catch {
        return @{ StatusCode = 0; Body = $_.Exception.Message }
    }
}

Write-Host '=== FlowWatch stack health ===' -ForegroundColor Cyan

$es = Get-JsonFromUrl -Url $esUrl
if ($es.StatusCode -eq 200) {
    $esJson = $es.Body | ConvertFrom-Json
    $esStatus = $esJson.status
    if ($esStatus -in @('green','yellow')) {
        Write-Host "ELASTICSEARCH: OK ($esStatus)" -ForegroundColor Green
    }
    else {
        Write-Host "ELASTICSEARCH: WARNING ($esStatus)" -ForegroundColor Yellow
    }
    Write-Host $es.Body
}
else {
    Write-Host "ELASTICSEARCH: FAIL (HTTP $($es.StatusCode))" -ForegroundColor Red
    Write-Host $es.Body
}

Write-Host '---'

$kibana = Get-JsonFromUrl -Url $kibanaUrl
if ($kibana.StatusCode -eq 200) {
    try {
        $kibanaJson = $kibana.Body | ConvertFrom-Json
        $kibanaStatus = $kibanaJson.status
        if ($kibanaStatus -in @('green','yellow')) {
            Write-Host "KIBANA: OK ($kibanaStatus)" -ForegroundColor Green
        }
        else {
            Write-Host "KIBANA: WARNING ($kibanaStatus)" -ForegroundColor Yellow
        }
        Write-Host $kibana.Body
    }
    catch {
        Write-Host 'KIBANA: OK (HTML reached, but JSON parse may differ by version)' -ForegroundColor Green
        Write-Host $kibana.Body.Substring(0, [Math]::Min(300, $kibana.Body.Length))
    }
}
else {
    Write-Host "KIBANA: FAIL (HTTP $($kibana.StatusCode))" -ForegroundColor Red
    Write-Host $kibana.Body
}

Write-Host '---'
Write-Host 'Docker services:' -ForegroundColor Cyan
& 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' compose ps
