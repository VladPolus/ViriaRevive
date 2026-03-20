' ViriaRevive — Silent Launcher (no console window)
' Double-click this file to start ViriaRevive without any terminal.

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

' Get the folder this script lives in
scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)

' Build paths
pythonw = scriptDir & "\venv\Scripts\pythonw.exe"
appFile = scriptDir & "\app.pyw"

' Check pythonw exists
If Not FSO.FileExists(pythonw) Then
    MsgBox "Python not found at:" & vbCrLf & pythonw & vbCrLf & vbCrLf & _
           "Make sure the venv is set up (run: python -m venv venv)", _
           vbExclamation, "ViriaRevive"
    WScript.Quit
End If

' Check app.pyw exists
If Not FSO.FileExists(appFile) Then
    MsgBox "app.pyw not found at:" & vbCrLf & appFile, _
           vbExclamation, "ViriaRevive"
    WScript.Quit
End If

' Launch with no visible window (0 = hidden, False = don't wait)
WshShell.CurrentDirectory = scriptDir
WshShell.Run """" & pythonw & """ """ & appFile & """", 0, False
