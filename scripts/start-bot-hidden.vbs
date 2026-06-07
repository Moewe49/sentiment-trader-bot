' start-bot-hidden.vbs
' Startet start-bot.ps1 voellig unsichtbar: kein Fenster, kein kurzes
' Aufblitzen einer Konsole. Der zweite Parameter 0 = verstecktes Fenster.
Dim sh, ps1
ps1 = "C:\Users\nikla\Downloads\sentiment-trader_4\sentiment-trader\scripts\start-bot.ps1"
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -NonInteractive -File """ & ps1 & """", 0, False
