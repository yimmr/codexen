Set FSO = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")
scriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = scriptDir

userProfile = WshShell.ExpandEnvironmentStrings("%USERPROFILE%")
pyw = "pythonw.exe"

If FSO.FileExists(scriptDir & "\.venv\Scripts\pythonw.exe") And (FSO.FolderExists(scriptDir & "\.venv\Lib\site-packages\starlette") Or FSO.FolderExists(scriptDir & "\.venv\Lib\site-packages\pystray")) Then
    pyw = scriptDir & "\.venv\Scripts\pythonw.exe"
ElseIf FSO.FileExists(userProfile & "\Miniconda3\pythonw.exe") Then
    pyw = userProfile & "\Miniconda3\pythonw.exe"
ElseIf FSO.FileExists(scriptDir & "\.venv\Scripts\pythonw.exe") Then
    pyw = scriptDir & "\.venv\Scripts\pythonw.exe"
End If

WshShell.Run """" & pyw & """ """ & scriptDir & "\tray_app.py""", 0, False
