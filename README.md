# TimeBackup

**定時創建永久備份**

- `!!auto-backup 幫助`
- `!!auto-backup status: 下次備份時間`
- `!!auto-backup enable: 開啟自動備份`
- `!!auto-backup disable: 關閉自動備份`
- `!!auto-backup make <備註(可選)>: 手動創建備份`
---
**使用方式**
- 1.打開MCDR伺服器的plugins資料夾
- 2.將time_backup.py丟入plugins資料夾內
---
**更改備份間隔時間**
- 1.使用任意文字編輯器打開time_backup.py
- 2.找第61行中的「interval: str = "2d"」
- 3.將"2d"改為任意時間 <br>
`s`代表秒　`ｍ`代表分鐘　`ｈ`代表小時　`ｄ`代表天
