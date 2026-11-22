# 机场行李错运交接采证台

离线运行的行李托运、分拣交接和旅客申领责任账本。服务机场地服、转运承运方和旅客服务台；所有输入是仓内合成 JSON，不连接机场系统、航司接口或真实旅客数据。

## 快速运行

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m bagchain demo --db demo.sqlite
python -m bagchain audit --db demo.sqlite
python -m unittest discover -s tests -v
```

分别导入三套业务证据：

```powershell
python -m bagchain bag-import fixtures/bag-ok.json --db state.sqlite
python -m bagchain sort-import fixtures/sort-ok.json --db state.sqlite
python -m bagchain claim-import fixtures/claim-ok.json --db state.sqlite
python -m bagchain report --db state.sqlite --bag BAG-1
python -m bagchain audit --db state.sqlite
```

三套失败夹具 `bag-bad.json`、`sort-bad.json`、`claim-bad.json` 分别触发禁运物品、错航班与缺交接、自报非本人领取。用另一份文件重复相同 event_id、不同正文会整文件回滚；同文件重放幂等。

## 离线读头采集（原始帧 -> 可导入 scan）

分拣口实际收到的是多天线 RFID 读数与条码枪短帧，不是已填妥的 scan 事件。`bagchain/reader.py` 从本地 mock 读头批次恢复“哪件行李在何时真正越过哪个分拣口”，再把身份闭合的轨迹经原有 `import_file('sort')` 投影成 scan/load，读头审计行与业务投影在同一 SQLite 事务原子提交：

```powershell
python -m bagchain bag-import fixtures/bag-ok.json --db state.sqlite
python -m bagchain bag-import fixtures/bag-bag2.json --db state.sqlite
python -m bagchain reader-import fixtures/reader-crossing.json --db state.sqlite --policy direct_first
python -m bagchain reader-import fixtures/reader-crossing.json --db state.sqlite --policy anti_missort
python -m bagchain report --db state.sqlite --bag BAG-1
```

管线步骤：①校准信标按读头重启纪元估计时钟偏移/漂移，未知纪元不猜时间；②补传帧按 frame_id 去重，CRC 失败帧保留标签身份但不可用；③EPC 绑定按时间窗解析，退役标签（标签复用）不绑定现行行李；④几何过滤只承认本闸本天线、达场强阈值且 CRC 正确的读数，邻闸远距离听到一律 `remote_overhear`，不能算过闸；⑤条码校验失败、条码与同场 RFID 身份不一致都保留为冲突帧，不凭最强信号猜测身份。

**为什么不能逐件取最高 RSSI**：交叉输入里两件行李的局部峰值都落在 G2，独立选闸必然同闸、违反一闸一件的物理容量；“同闸只留第一件”补丁还会随导入顺序改变 BAG-1 的航班。联合关联枚举全部 bag→gate 指派，在时钟误差内同时满足单闸容量、传送带方向（IN/GATE/OUT 单调）、开放窗与身份边界；所有可行与不可行指派都作为假设落库（`reader_hypothesis`，含逐件 naive 基线行），多条全局关联同分时整批待核，不按文件顺序任取。

**两条事前授权路线（采集前由 `--policy` 固定，不靠调阈值）**：

| 路线 | 闭合条件 | 交叉输入的结论 |
| --- | --- | --- |
| `direct_first` 直达优先 | 唯一联合指派 + 校准后锚定本闸的有效条码 | BAG-1 在 G1 闭合为 ORIGIN/FL100 的 accepted scan 并 confirmed 装机；BAG-2 条码不足（校验失败+身份冲突）待核 |
| `anti_missort` 防错运 | 每件需闸前 RFID 与闸后条码两个独立身份锚点 | BAG-1 只有闸后条码（闸前 IN 帧碰撞 CRC 失败），缺 `pre_gate_rfid_in`；BAG-2 缺 `post_gate_barcode`；两件均待核，BAG-1 的 load 也不产生 |

`reader-ambiguous.json` 是不可判定边界：两条全局关联物理证据完全同分，任何路线都不产生 scan，帧与假设仍全部留存可审计。


## 独立业务决定

| 子系统 | 持久实体 | 主要结论 |
| --- | --- | --- |
| 托运身份与合同 | booking、bag_tag | 客票身份、目的地、重量与禁运物决定可否签收；合同版本取消或改目的地会使后续责任重裁。 |
| 分拣与转运责任 | scan、load_manifest、handoff、arrival | 航班匹配、扫描时序、装机位置和双方交接决定保管责任能否转移；未证实到达不得按已送达处理。 |
| 旅客申领与补偿 | claim、decision | 合法领取人、到达证据、独立损伤证明和合同上限决定领取、追送或赔付。 |

三条跨账路径：①托运目的地更改使旧交接与旧到达转争议，进而让已结领取进入复核；②分拣缺失装机证明或双方交接使到达不可证实，领取被阻断；③到达状态和授权领取身份共同决定是否结案，超过承诺时间的追送在迟到到达后转为有上限的赔付。`audit` 使用原始文件字节与 SHA-256 在新数据库重放全部实体与决定，发现摘要或投影差异会报告异常。导入与投影使用同一 SQLite 事务。

当前为单机离线演示：工作人员身份、航班信息和损伤证明仅是合成输入标签；没有外部签名验证、真实航空规则或付款执行。规模按此业务范围实现，测试通过不代表真实机场合规认证。
