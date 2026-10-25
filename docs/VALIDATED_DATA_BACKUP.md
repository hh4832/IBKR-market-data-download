# 驗證後的資料與雲端備份

GitHub 保存程式、離線測試與操作政策。行情 Parquet、執行日誌、checkpoint、私人 recovery 計畫及逐筆回覆留在私人硬碟／雲端備份；不要 commit。

## 核心資料的優先順序

1. **整個最新版整合資料夾**：包含 `union_annotated/NQ`、`union_annotated/ES`、`research_positive_volume/NQ`、`research_positive_volume/ES`、`output_manifest.csv`、`contract_summary.csv`、`dataset_summary.json`、`BUILD_COMPLETE.json`、缺口帳與說明。只保存研究視圖會失去被篩掉的零量／到期後列和稽核來源。
2. **證據與驗證報告**：逐分鐘 resolution ledger、休市規則／公告、前後時段及逐筆查詢證據、雜湊、輸入驗證 manifest。來源無成交解釋必須連同回覆與請求保存，不能只留「0 缺口」摘要。
3. **可重建來源**：原始已驗證快照、完整 NQ／ES recovery run（含 Parquet、results、plan）；這些是還原與重算的來源。原快照含其他標的，不要誤把 NQ／ES 結案套用至股票。
4. **程式與環境**：GitHub repo／commit、實際使用的本機工具與操作記錄、Conda explicit list 或 environment export。私人參數及路徑放私人備份。

`union_annotated` 是來源＋補回的逐合約聯集；`research_positive_volume` 是明示過濾政策的研究視圖。它們不是換月後的連續期貨，也沒有回溯調整。到期／零量／時段過濾須依研究目的再決定。

## 剩餘分鐘如何結案

- `EXPLAINED_CLOSED`：精確休市起訖與可追溯公告，保留來源權威層級；券商歷史公告不可冒稱交易所原始公告。
- `RECOVERED_REAL_BAR`：實際保存、通過識別／時間／價格／雜湊檢查的 K 棒。
- `EXPLAINED_IB_FEED_NO_REPORTED_TRADES`：從缺口起點向後查 historical TRADES ticks，成功完成、無錯誤且回覆非空，首筆在缺口末端或之後；同時保留較寬分鐘查詢佐證。**只說明 IB 來源未報成交，不證明整個交易所完全沒有成交。**
- 空回覆、API 錯誤、不完整回覆、區間內有 tick 卻無 bar：繼續待查，不強制歸零、不補造 OHLCV。

`ibkr_gap_evidence.assess_forward_ticks` 是離線判讀函式。呼叫者必須先確認 exact contract、forward start、TRADES、useRTH=False 與完成回呼，保存請求及回覆。函式不連線、不更改資料，也不自動認證交易所完整性。

2026-10-25 本機結案案例：兩商品各 12,780 分鐘休市解釋、11,565 分鐘補回，另 NQ 25／ES 3 分鐘有逐筆來源證據。原計畫／IB 來源口徑未解釋為 0；18 個合約、4,724 個輸入、36 個整合 Parquet 已在本機檢查。這些是特定資料快照結果，**GitHub CI 沒有讀取這些私人資料，也不能替其他資料集背書**。資料依原計畫截至 2026-10-20，未刷新到現在。

## 建立可驗證的私人 ZIP

在 Windows Anaconda Prompt，先預覽（不改來源、不產生檔案）：

```bat
python -B verified_backup.py --source dataset "D:\PrivateData\validated_dataset" --source evidence "D:\PrivateData\resolution_evidence" --destination "D:\Backups\IBKR_core_20261025.zip"
```

確認選取範圍後，同一命令加 `--execute`。來源資料夾不能重疊；目的地必須在所有來源外、父目錄已存在且檔名尚不存在；連結／junction／Windows reparse point 拒絕。程式只新增目的 ZIP，任何失敗都不會留下成功憑證，未完成 ZIP 不能當有效備份。

工具逐檔串流 SHA256，ZIP 寫入後重讀核對，再重讀來源確認內容與修改時間未變；內含可攜的相對路徑 `BACKUP_MANIFEST.json`。來源檔案系統不提供鎖定快照，執行期間應停止所有下載／改檔程序。

上傳私人雲端後，重新下載到另一個路徑驗證：

```bat
python -B verified_backup.py --verify "D:\Restored\IBKR_core_20261025.zip"
```

這驗證 ZIP 內各檔與內含 manifest 一致；再將整包 SHA256 與建立當時保存在另一處的回執比較，才能核對整包是否被替換。雲端顯示上傳完成，不等於已完成還原驗證。ZIP 不加密，請使用私人雲端存取設定。

本次 GitHub 更新提供可重用的證據判讀與標準庫備份工具；特定本機資料盤點、假日日曆與工作目錄的執行腳本應連同私人備份保存，不冒充從全新 clone 就能重建的公開範例。
