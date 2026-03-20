' ViriaRevive — Startup Launcher (auto-launch minimized to tray)
' This is placed in the Windows Startup folder to auto-run on login.
' The app starts hidden in the system tray — click the tray icon to open.

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)

' Build paths
pythonw = scriptDir & "\venv\Scripts\pythonw.exe"
appFile = scriptDir & "\app.pyw"

' Silently exit if files are missing (don't annoy on startup)
If Not FSO.FileExists(pythonw) Then WScript.Quit
If Not FSO.FileExists(appFile) Then WScript.Quit

' Launch minimized to tray (--minimized flag)
WshShell.CurrentDirectory = scriptDir
WshShell.Run """" & pythonw & """ """ & appFile & """ --minimized", 0, False
