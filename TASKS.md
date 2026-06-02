# TASKS

מסמך מעקב משימות חי. סמן `[x]` כשמשימה הושלמה; הוסף משימות חדשות מתחת תוך כדי עבודה.

## Todo
- [ ] `cc-history serve` / v2 items (out of scope for v1)
- [ ] בדיקה על macOS (LaunchAgent + shim ל-`~/.local/bin`) — נבדק רק על Windows עד כה

## In progress
- (אין)

## Done
- [x] אתחול הפרויקט + git repo תחת `C:\Users\orben\OneDrive\DEV\Projects\cc-history`
- [x] `cc_history.py` v1 מלא — Paths, סכמת DB + FTS5, פרסר JSONL, indexer אינקרמנטלי, search/show/stats, שני backends של Scheduler, CLI
- [x] `tests/fixtures/sample-session.jsonl` — מכסה כל סוגי הבלוקים (text/thinking/tool_use/tool_result/image-skip/stringified)
- [x] `tests/test_indexer.py` — 18 בדיקות, כולן עוברות (כולל crash-resilience #10)
- [x] README.md, LICENSE (MIT), .gitignore, install.sh, install.ps1
- [x] סבב בדיקות שפיות מלא על Windows: index (אינקרמנטלי), stats, search, show, --json, חיפוש עברית, install/kickstart/uninstall
- [x] תיקון באג אמיתי: `<LogonTrigger>` בלי `<UserId>` גרם ל-`Access is denied`. תוקן ב-`cc_history.py` + עודכן האיפיון §8b
- [x] תיקון: כפיית UTF-8 על stdout/stderr ב-Windows כדי שפלט עברית/`·`/`…` לא יקרוס על cp1252
- [x] Entry-point shim (Appendix B) — `install` יוצר `cc-history.cmd` ומוסיף ל-user PATH דרך הרג'יסטרי (לא setx) עם fallback; backend מקביל ל-macOS (`~/.local/bin/cc-history`). נבדק על Windows: הפקודה `cc-history` עובדת.
- [x] בדיקת sanity #10 (crash-resilience) — נוספה כבדיקת יחידה: כשל באמצע קובץ מגלגל לאחור הכול ומשאיר `last_offset`, וריצה חוזרת לא מכפילה ספירה.

## Notes
- מפרש Python בשימוש: `C:\Users\orben\AppData\Local\Programs\Python\Python311\python.exe` (ה-`python` ב-PATH הוא ה-stub של חנות Microsoft ולא עובד).
- אינדקס נוכחי על המכונה: ~9,300 רשומות מ-22 קבצים.
- **הכלי מותקן כרגע בפועל על המכונה הזו**: scheduled task `cc-history\orben-indexer` (כל 10 דק') + `%LOCALAPPDATA%\cc-history` נוסף ל-user PATH. הסרה: `cc-history uninstall` (משאיר את ה-DB).
