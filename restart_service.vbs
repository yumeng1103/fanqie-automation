' 番茄控制台服务 - 静默重启(无任何窗口): 杀掉旧进程 -> 启动新进程
' 用法: 双击本文件, 或命令行运行 wscript restart_service.vbs
Set ws = CreateObject("Wscript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
py = baseDir & "\.venv\Scripts\pythonw.exe"
ap = baseDir & "\app.py"
ws.Run "powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command ""& { Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.Name -match '^pythonw\.exe$' -and $_.CommandLine -match 'app\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 2; Start-Process -FilePath '" & py & "' -ArgumentList '" & ap & "' -WindowStyle Hidden }""", 0, False
