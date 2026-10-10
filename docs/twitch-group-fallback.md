# GroupID 未取得時的 Twitch 收錄

既有 Twitch 新遊戲入選與 IGDB external game / Steam AppID 映射，仍須由不可變的前端 commit 驗證；分類總觀眾門檻維持 7,000。遊戲本體、官方台灣確切發售日期與成人排除等 Steam 驗證不變。

匯入器先重用官方 Followers 快取，包括真正的 0；再檢查 checkpoint、待處理與先前保留的候選中已有的 GroupID（包含已驗證的 GroupID64 別名與短 ID）。已有 GroupID 但未取得 Followers 時，仍等待官方 Followers。沒有 GroupID 與可用數字時，符合完整 Twitch 資格的本作直接收錄，不再由 Community XML 阻擋。

此收錄的 `followers`、`follower_checked_at`、`follower_source` 都是 JSON `null`，`official_ge5000` 是 `false`，`follower_status` 是 `unavailable_group_id`，並保存不早於 Twitch 映射查核時間的 `follower_unavailable_at`。GroupID64 及其別名不能帶入其他作品的群組。未取得不能轉成 0，也不能宣稱通過 Steam 5,000 門檻。

成功套用至 master 後，同一 AppID 的一般、Twitch 與停放佇列完成；checkpoint 的 `twitch_admissions` 保存完整收錄證據，避免每日候選將它反覆排回缺群組工作。其他 AppID、官方結果、進度、冷卻與嘗試事件不變。沒有有效 Twitch 證據的缺 GroupID 遊戲依然等待；公開佇列以 `fallback_status=awaiting_twitch_qualification` 說明待等的是 Twitch 資格。

內容刷新事件傳送完整 Twitch 證據及以上 null/狀態欄位；分片、月曆、舊版清單與精簡 catalog 都保留這組證據。已有真正數字不能被缺 GroupID 的觀測抹除；恢復真正官方數字時移除未取得標記。Git 發布重試重用已凍結來源，保留並行新增的官方數值與內容，不重新採集；不變結果維持 no-op。

目前 MW4 必須等待本作分類實際達標，且有正確的 IGDB → Steam 本體映射。此機制不借用共用《Call of Duty》分類數字，也不覆寫 Steam DLC 排除或強制收錄任何特定作品。
