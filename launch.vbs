' =====================================================================
'  CBT - silent desktop launcher  (Option A)
'
'  Double-clicking a .vbs runs it through wscript.exe, which owns no
'  console. Everything it starts is therefore genuinely windowless -
'  unlike a .bat, which always flashes a console window even when the
'  script itself is silent.
'
'  First-run handling is the reason this is more than a one-liner:
'  building the virtual environment installs torch from the local
'  wheelhouse and takes minutes. Launching that completely silently
'  looks identical to "nothing happened", so the user double-clicks
'  again. This script therefore detects the first run, tells the user
'  what to expect, and shows a visible setup window for that run only.
'  Every subsequent launch is fully silent.
' =====================================================================

Option Explicit

Dim shell, fso, scriptDir, nativeHost, pythonExe, pythonwExe, bootstrap
Dim venvPython, isFirstRun, command, answer

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir  = fso.GetParentFolderName(WScript.ScriptFullName)
nativeHost = fso.BuildPath(scriptDir, "CBT\native_host")
pythonExe  = fso.BuildPath(nativeHost, "Python310\python.exe")
pythonwExe = fso.BuildPath(nativeHost, "Python310\pythonw.exe")
bootstrap  = fso.BuildPath(nativeHost, "bootstrap.py")

' ---- sanity checks -------------------------------------------------
If Not fso.FileExists(bootstrap) Then
    MsgBox "CBT installation is incomplete." & vbCrLf & vbCrLf & _
           "Missing: " & bootstrap, vbCritical, "CBT"
    WScript.Quit 2
End If

If Not fso.FileExists(pythonExe) Then
    MsgBox "CBT installation is incomplete." & vbCrLf & vbCrLf & _
           "Missing: " & pythonExe, vbCritical, "CBT"
    WScript.Quit 2
End If

' ---- first-run detection -------------------------------------------
' Mirrors launcher/config.py: %LOCALAPPDATA%\CBT_venv\venv
venvPython = fso.BuildPath(shell.ExpandEnvironmentStrings("%LOCALAPPDATA%"), _
                           "CBT_venv\venv\Scripts\python.exe")
isFirstRun = Not fso.FileExists(venvPython)

If isFirstRun Then
    answer = MsgBox( _
        "CBT needs to complete a one-time setup." & vbCrLf & vbCrLf & _
        "This installs the speech engine on this computer and can take " & _
        "several minutes. It runs entirely offline and only happens once." & _
        vbCrLf & vbCrLf & "Continue?", _
        vbOKCancel + vbInformation, "CBT - First-time setup")

    If answer <> vbOK Then WScript.Quit 0

    ' Visible window for the first run so progress is observable.
    ' 1 = normal window, True = wait for completion.
    command = """" & pythonExe & """ """ & bootstrap & """ --setup-only"
    If shell.Run(command, 1, True) <> 0 Then
        MsgBox "CBT setup failed." & vbCrLf & vbCrLf & _
               "See the logs in:" & vbCrLf & _
               shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & _
               "\CBT_venv\state\logs", vbCritical, "CBT"
        WScript.Quit 3
    End If
End If

' ---- normal silent launch ------------------------------------------
' pythonw.exe never allocates a console; window style 0 hides the
' process regardless. The launcher polls the server and opens the
' browser the moment it is ready.
If fso.FileExists(pythonwExe) Then
    command = """" & pythonwExe & """ """ & bootstrap & """"
Else
    command = """" & pythonExe & """ """ & bootstrap & """"
End If

' False = do not block; the launcher supervises the server from here on.
shell.Run command, 0, False

WScript.Quit 0
