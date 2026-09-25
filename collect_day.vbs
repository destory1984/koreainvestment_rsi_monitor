' 윈도우 작업 스케줄러가 창을 띄우지 않고 주간거래 분봉 수집(kis_replay.py --collect)을 돌리게 한다.
' 한국투자증권 키가 ~/.bashrc 에 있어서 Git Bash 로 돌린다. Git 을 다른 곳에 깔았으면 BASH 를 고칠 것.
Const BASH = "C:\Program Files\Git\bin\bash.exe"
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
cmd = "source ~/.bashrc >/dev/null 2>&1; cd '" & here & "' && PYTHONIOENCODING=utf-8 python kis_replay.py --collect"
CreateObject("WScript.Shell").Run """" & BASH & """ -c """ & cmd & """", 0, True
