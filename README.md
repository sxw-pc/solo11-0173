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

## 独立业务决定

| 子系统 | 持久实体 | 主要结论 |
| --- | --- | --- |
| 托运身份与合同 | booking、bag_tag | 客票身份、目的地、重量与禁运物决定可否签收；合同版本取消或改目的地会使后续责任重裁。 |
| 分拣与转运责任 | scan、load_manifest、handoff、arrival | 航班匹配、扫描时序、装机位置和双方交接决定保管责任能否转移；未证实到达不得按已送达处理。 |
| 旅客申领与补偿 | claim、decision | 合法领取人、到达证据、独立损伤证明和合同上限决定领取、追送或赔付。 |

三条跨账路径：①托运目的地更改使旧交接与旧到达转争议，进而让已结领取进入复核；②分拣缺失装机证明或双方交接使到达不可证实，领取被阻断；③到达状态和授权领取身份共同决定是否结案，超过承诺时间的追送在迟到到达后转为有上限的赔付。`audit` 使用原始文件字节与 SHA-256 在新数据库重放全部实体与决定，发现摘要或投影差异会报告异常。导入与投影使用同一 SQLite 事务。

当前为单机离线演示：工作人员身份、航班信息和损伤证明仅是合成输入标签；没有外部签名验证、真实航空规则或付款执行。规模按此业务范围实现，测试通过不代表真实机场合规认证。
