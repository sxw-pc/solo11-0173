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

## 多通道混合材质的有界复核

三通道扫描只保存体素原始三通道读数、厚度与遮蔽、参考版本、同次设备校准和体素体积。参考库合成三种材质：受限材质 `R=[4,6,2]`、普通软包装 `P=[7,2,5]`、无受限成分的复合洗漱用品 `C=[5.5,4,3.5]`，且 `C=(R+P)/2`。因此读数 `[5.5,4,3.5]` 既可由 100% C 也可由 R/P 各半以零残差产生，两种装包的数组、SHA 和单材质最近邻完全相同，R 体积分数却分别是 0 和 0.5。系统不任选一个零残差分解，而是保留全部可行组成与全包体积分数上下界。

- 暂定标签（`screening.label`）仍只对同包参考做欧氏最近邻，状态恒为 `provisional`；参考库改变会改变标签，但标签和 `booking.items` 旅客申报都不是受限成分的实测份额，新分解不会覆盖它们。
- 一次设备校准偏差由整批体素共用：联合线性规划只有一个偏差向量，体素不能各自挑选对自己有利的偏差；逐体素独立最坏值相加（`independent_bag_bounds`）是留作对照的弱口径，其区间只会更宽。遮蔽体素没有分数证据，按全包 `[0,1]` 计入并进入补扫位置，任何路线都不能把无证体素写成已放行。
- 路线 A 用带第四能量通道的合格设备复扫：第四通道满秩时无遮挡体素得到唯一分解，金属遮蔽区仍为待核；复扫证据钉住设备号与标定版本，日后换标定只产生新版本，不改写当时的开包决定。
- 路线 B 仅在旅客在场、双人记录下取外层口袋封存拭子：检出 R 只触发该口袋人工复核，不能向内推算全包体积分数；未检出也不改变内层区间。
- 人工开包结果以 `manual_open` 与此前所有待核版本并列留痕，旧版本不删除、不回写。

`fixtures/screen-mixture.json` 提供三通道批次（含一个金属遮蔽体素）、路线 A 复扫和路线 B 拭子的合成证据；`python -B -m pytest -q -p no:cacheprovider` 覆盖暂定标签随参考库变化、同读数反例、共享校准区间、两条补采路线的体积覆盖和 audit 篡改检出。

