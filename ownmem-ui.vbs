' =====================================================================
'  OwnMem 审计台 · 静默启动器 (v3)
' ---------------------------------------------------------------------
'  双击之后：
'    · 服务已在运行  -> 直接打开浏览器，不弹任何窗口
'    · 服务没在运行  -> 后台隐藏启动，等它就绪再打开浏览器
'    · 环境不对      -> 弹窗说明缺什么（不再让你干等 12 秒）
'    · 启动失败      -> 弹窗说明原因（附日志尾部 + 一键打开日志）
'  停止服务：网页右上角「停止服务」按钮
'  排障备用：项目里的 ownmem-ui.cmd（会留黑窗口显示报错）
'
'  v3 相比 v2 改了什么
'    · 项目目录不再写死 C:\Users\xxx\ownmem
'      默认 %USERPROFILE%\ownmem，也可用 OWNMEM_DIR 环境变量指向别处
'    · 找 Python 不再只看几个写死的路径
'      补了 py -3 启动器、3.11/3.12/3.13 常见位置、D 盘 miniconda；
'      而且每个候选都"真跑一次"再采纳，避免拿到 Microsoft Store 的空壳
'      python —— 那种 python 文件存在、一执行就弹商店且不报错，最难排查
'    · 环境不对时立刻说清楚，不再等满 12 秒才告诉你失败
' =====================================================================

Const PORT = 8787            ' 审计台端口
Const WAIT_ROUNDS = 24       ' 每轮 500ms，共 12 秒等待
Const LOG_MAX = 1048576      ' 日志超过 1MB 就轮转一次

Dim ws, fso, home, url, py, logfile, logbak, cmd, ok, i, msg, tail

Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

url = "http://127.0.0.1:" & PORT & "/"

home = ResolveHome()
logfile = home & "\webui.log"
logbak  = home & "\webui.log.1"

' ---- 先确认环境，别等超时才发现 ----
If Not fso.FolderExists(home) Then
  MsgBox "找不到 OwnMem 项目目录：" & vbCrLf & vbCrLf & home & vbCrLf & vbCrLf & _
         "如果项目放在别处，请设置一个叫 OWNMEM_DIR 的环境变量指向它，" & vbCrLf & _
         "或者直接改这个启动器里的 ResolveHome。", vbCritical, "OwnMem 审计台"
  WScript.Quit 1
End If

py = FindPython()
If Len(py) = 0 Then
  MsgBox "没找到能用的 Python（需要 3.10 以上）。" & vbCrLf & vbCrLf & _
         "试过这些地方：" & vbCrLf & _
         "　· " & home & "\.venv\Scripts\python.exe" & vbCrLf & _
         "　· %LOCALAPPDATA%\Programs\Python\Python31x\python.exe" & vbCrLf & _
         "　· D:\miniconda3\python.exe" & vbCrLf & _
         "　· py -3（Python 启动器）" & vbCrLf & _
         "　· PATH 里的 python" & vbCrLf & vbCrLf & _
         "注意：Windows 商店版的 python 是个空壳，文件在但跑不起来，不算。", _
         vbCritical, "OwnMem 审计台"
  WScript.Quit 1
End If

ok = ProbeAlive()

If Not ok Then
  RotateLog
  SetEnv
  cmd = BuildCmd(py)
  ws.Run cmd, 0, False

  For i = 1 To WAIT_ROUNDS
    WScript.Sleep 500
    If ProbeAlive() Then
      ok = True
      Exit For
    End If
  Next
End If

If ok Then
  ws.Run url, 1, False
Else
  msg = "OwnMem 审计台启动失败。" & vbCrLf & vbCrLf
  msg = msg & "Python ：" & py & vbCrLf
  msg = msg & "项目　：" & home & vbCrLf
  msg = msg & "端口　：" & PORT & vbCrLf
  msg = msg & "命令　：" & cmd & vbCrLf & vbCrLf
  msg = msg & "常见原因：" & vbCrLf
  msg = msg & "　1. " & PORT & " 端口被别的程序占用" & vbCrLf
  msg = msg & "　2. 代码报错（详见日志）" & vbCrLf & vbCrLf
  tail = LogTail(8)
  If Len(tail) > 0 Then
    msg = msg & "—— 日志尾部 ——" & vbCrLf & tail & vbCrLf
  End If
  msg = msg & "点「确定」打开 webui.log 看完整报错（发给帮你排查的人）；" & vbCrLf
  msg = msg & "点「取消」关闭。也可以双击 ownmem-ui.cmd 看黑窗口里的报错。"

  If MsgBox(msg, vbCritical + vbOKCancel, "OwnMem 审计台") = vbOK Then
    If fso.FileExists(logfile) Then
      ws.Run "notepad.exe """ & logfile & """", 1, False
    Else
      MsgBox "连日志文件都没生成，说明启动命令根本没跑起来。" & vbCrLf & vbCrLf & _
             "先手动执行这一条看看报什么：" & vbCrLf & _
             py & " -m ownmem.cli ui --port " & PORT, _
             vbInformation, "OwnMem 审计台"
    End If
  End If
End If

' ---------------------------------------------------------------------
' 项目目录：OWNMEM_DIR 环境变量优先，默认 %USERPROFILE%\ownmem
' ---------------------------------------------------------------------
Function ResolveHome()
  Dim envHome
  envHome = ws.ExpandEnvironmentStrings("%OWNMEM_DIR%")
  ' 变量没定义时，展开结果里还带着 %，据此判空
  If InStr(envHome, "%") > 0 Then envHome = ""
  If Len(envHome) > 0 Then
    ResolveHome = envHome
    Exit Function
  End If
  ResolveHome = fso.BuildPath(ws.ExpandEnvironmentStrings("%USERPROFILE%"), "ownmem")
End Function

' ---------------------------------------------------------------------
' 探测：8787 上是不是已经有 OwnMem 在服务
' 每个请求新建对象，且先判 Err 再读 Status（VBScript 的 And 不短路，
' 连接失败时读 Status 会拿到脏值）
' ---------------------------------------------------------------------
Function ProbeAlive()
  Dim h, good
  good = False
  On Error Resume Next
  Set h = CreateObject("MSXML2.ServerXMLHTTP.6.0")
  h.setTimeouts 800, 800, 800, 1500
  h.Open "GET", url & "api/overview", False
  h.Send
  If Err.Number = 0 Then
    If h.Status = 200 Then
      If InStr(h.ResponseText, """stats""") > 0 Then good = True
    End If
  End If
  Err.Clear
  On Error GoTo 0
  ProbeAlive = good
End Function

' ---------------------------------------------------------------------
' 这个 Python 命令真能跑吗（跑一次 import sys，看退出码）
' 光看文件在不在不够：Microsoft Store 的 python 是空壳，
' 文件存在、一执行就弹商店，是这类启动器最难排查的坑。
' ---------------------------------------------------------------------
Function PyWorks(pycmd)
  Dim o, waited, code
  PyWorks = False
  On Error Resume Next
  Set o = ws.Exec("cmd /c """ & pycmd & """ -c ""import sys"" >nul 2>&1")
  If Err.Number <> 0 Then
    Err.Clear
    On Error GoTo 0
    Exit Function
  End If
  waited = 0
  Do While o.Status = 0 And waited < 40
    WScript.Sleep 100
    waited = waited + 1
  Loop
  If o.Status = 0 Then o.Terminate
  code = o.ExitCode
  On Error GoTo 0
  PyWorks = (code = 0)
End Function

' ---------------------------------------------------------------------
' 找 Python：项目内 venv > 常见安装位置 > py 启动器 > PATH
' 返回空串表示没找到可用解释器
' ---------------------------------------------------------------------
Function FindPython()
  Dim cands, k, localApp
  localApp = ws.ExpandEnvironmentStrings("%LOCALAPPDATA%")
  cands = Array( _
    home & "\.venv\Scripts\python.exe", _
    localApp & "\Programs\Python\Python313\python.exe", _
    localApp & "\Programs\Python\Python312\python.exe", _
    localApp & "\Programs\Python\Python311\python.exe", _
    "D:\miniconda3\python.exe", _
    "C:\miniconda3\python.exe")

  For k = 0 To UBound(cands)
    If fso.FileExists(cands(k)) Then
      FindPython = cands(k)
      Exit Function
    End If
  Next

  ' py 启动器：装了 Python 的 Windows 基本都有，自己会挑最新版
  If PyWorks("py -3") Then
    FindPython = "py -3"
    Exit Function
  End If
  ' 最后才试 PATH 里的裸 python，而且要真跑一次，确认它不是商店空壳
  If PyWorks("python") Then
    FindPython = "python"
    Exit Function
  End If

  FindPython = ""
End Function

' ---------------------------------------------------------------------
' 生成启动命令：外面额外套一层引号，绕开 cmd /c 砍首尾引号的规则。
' py 命令本身带空格（py -3），所以解释器路径统一也套一层引号。
' ---------------------------------------------------------------------
Function BuildCmd(p)
  Dim c
  c = "cmd /c """ & """" & p & """"
  c = c & " -m ownmem.cli ui --port " & PORT & " --no-browser > "
  c = c & """" & logfile & """"
  c = c & " 2>&1"""
  BuildCmd = c
End Function

' ---------------------------------------------------------------------
' 日志轮转：超过 1MB 就挪成 webui.log.1
' ---------------------------------------------------------------------
Sub RotateLog()
  On Error Resume Next
  If fso.FileExists(logfile) Then
    If fso.GetFile(logfile).Size > LOG_MAX Then
      If fso.FileExists(logbak) Then fso.DeleteFile logbak, True
      fso.MoveFile logfile, logbak
    End If
  End If
  Err.Clear
  On Error GoTo 0
End Sub

' ---------------------------------------------------------------------
' 给将要启动的子进程注入环境变量
' ---------------------------------------------------------------------
Sub SetEnv()
  Dim e
  Set e = ws.Environment("PROCESS")
  e("PYTHONPATH") = home
  e("PYTHONUTF8") = "1"
  e("PYTHONIOENCODING") = "utf-8"
  e("PYTHONUNBUFFERED") = "1"
End Sub

' ---------------------------------------------------------------------
' 读日志尾部（用 ADODB.Stream 按 UTF-8 读，中文不会乱；失败就返回空）
' ---------------------------------------------------------------------
Function LogTail(n)
  Dim st, s, arr, k, start, out
  LogTail = ""
  If Not fso.FileExists(logfile) Then Exit Function
  On Error Resume Next
  Set st = CreateObject("ADODB.Stream")
  st.Type = 2
  st.Charset = "utf-8"
  st.Open
  st.LoadFromFile logfile
  s = st.ReadText(-1)
  st.Close
  If Err.Number <> 0 Then
    Err.Clear
    On Error GoTo 0
    Exit Function
  End If
  On Error GoTo 0
  arr = Split(Replace(s, vbCr, ""), vbLf)
  start = UBound(arr) - n + 1
  If start < 0 Then start = 0
  out = ""
  For k = start To UBound(arr)
    If Len(Trim(arr(k))) > 0 Then out = out & arr(k) & vbCrLf
  Next
  If Len(out) > 700 Then out = Right(out, 700)
  LogTail = out
End Function
