# 使用已安装的Windows中文语音，只返回WAV数据，不打开扬声器。
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Speech
$eyesSynth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $eyesSynth.SelectVoice('Microsoft Huihui Desktop')
    [Console]::WriteLine('{"ready":true,"voice":"Microsoft Huihui Desktop"}')
    while ($null -ne ($eyesLine = [Console]::ReadLine())) {
        $eyesRequest = $eyesLine | ConvertFrom-Json
        $eyesStream = New-Object System.IO.MemoryStream
        try {
            $eyesSynth.SetOutputToWaveStream($eyesStream)
            $eyesSynth.Speak([string]$eyesRequest.text)
            $eyesSynth.SetOutputToNull()
            $eyesReply = @{
                id = $eyesRequest.id
                wav = [Convert]::ToBase64String($eyesStream.ToArray())
            } | ConvertTo-Json -Compress
            [Console]::WriteLine($eyesReply)
        }
        catch {
            [Console]::WriteLine((@{ id = $eyesRequest.id; error = 'Installed voice synthesis failed' } | ConvertTo-Json -Compress))
        }
        finally {
            $eyesStream.Dispose()
        }
    }
}
finally {
    $eyesSynth.Dispose()
}
