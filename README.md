# 投标报价模拟器

一个基于固定评标规则的投标报价模拟工具。

## 功能

- 输入控制价、平均下浮率、主流下浮区间、竞争家数和随机系数范围
- 按固定规则模拟多轮竞争报价
- 输出推荐下浮率、推荐报价和候选结果排名

评标规则：

1. 剔除一个最高价
2. 剔除一个最低价
3. 剩余报价取平均
4. 平均值乘随机系数得到基准价
5. 最接近基准价者中标

## 本地运行

默认启动桌面界面：

```bash
python bid_simulator.py
```

命令行模式：

```bash
python bid_simulator.py --cli --simulations 1000
```

## Windows 打包

项目内置 GitHub Actions，会在 `push` 到主分支后自动构建 Windows `.exe`。

生成产物位于 Actions Artifacts，文件名为：

- `bid-simulator-windows`

解压后运行：

- `bid_simulator.exe`
