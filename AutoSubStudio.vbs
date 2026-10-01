Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

Folder = FSO.GetParentFolderName(WScript.ScriptFullName)
BatFile = Folder & "\START.bat"

WshShell.Run Chr(34) & BatFile & Chr(34), 0, False