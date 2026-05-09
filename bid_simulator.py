#!/usr/bin/env python3
"""Bid simulator for the pricing rule described by the user."""

from __future__ import annotations

import argparse
import traceback
import random
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from dataclasses import dataclass


@dataclass
class ScenarioConfig:
    control_price: float
    avg_discount: float = 0.0225
    discount_low: float = 0.018
    discount_high: float = 0.026
    competitor_min: int = 30
    competitor_typical_low: int = 35
    competitor_typical_high: int = 48
    a_percent: float = 0.97
    sample_total_count: int = 10
    simulations: int = 5000
    candidate_step: float = 0.0005
    candidate_padding: float = 0.003
    seed: int | None = 42


def parse_args() -> ScenarioConfig:
    parser = argparse.ArgumentParser(
        description="Simulate a bid scene and search for the best own discount."
    )
    parser.add_argument(
        "--control-price",
        type=float,
        default=120_000_000,
        help="Maximum control price, e.g. 120000000 for 1.2e8.",
    )
    parser.add_argument(
        "--avg-discount",
        type=float,
        default=2.25,
        help="Average discount percentage, e.g. 2.25 means 2.25%%.",
    )
    parser.add_argument(
        "--discount-low",
        type=float,
        default=1.8,
        help="Mainstream discount lower bound percentage.",
    )
    parser.add_argument(
        "--discount-high",
        type=float,
        default=2.6,
        help="Mainstream discount upper bound percentage.",
    )
    parser.add_argument(
        "--competitor-min",
        type=int,
        default=30,
        help="Lower bound for competitor count.",
    )
    parser.add_argument(
        "--competitor-typical-low",
        type=int,
        default=35,
        help="Typical lower bound for competitor count.",
    )
    parser.add_argument(
        "--competitor-typical-high",
        type=int,
        default=48,
        help="Typical upper bound for competitor count.",
    )
    parser.add_argument(
        "--a-percent",
        type=float,
        default=97,
        help="A percent value, e.g. 97 means A%=97%%.",
    )
    parser.add_argument(
        "--sample-total-count",
        type=int,
        default=10,
        help="Total sampled bidder count across two groups.",
    )
    parser.add_argument(
        "--simulations",
        type=int,
        default=5000,
        help="Simulation count for each candidate discount.",
    )
    parser.add_argument(
        "--candidate-step",
        type=float,
        default=0.05,
        help="Candidate discount step in percentage points, e.g. 0.01 means 0.01%%.",
    )
    parser.add_argument(
        "--candidate-padding",
        type=float,
        default=0.3,
        help="Search outside the mainstream range by this many percentage points.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible output.",
    )

    args = parser.parse_args()
    return ScenarioConfig(
        control_price=args.control_price,
        avg_discount=args.avg_discount / 100.0,
        discount_low=args.discount_low / 100.0,
        discount_high=args.discount_high / 100.0,
        competitor_min=args.competitor_min,
        competitor_typical_low=args.competitor_typical_low,
        competitor_typical_high=args.competitor_typical_high,
        a_percent=args.a_percent / 100.0,
        sample_total_count=args.sample_total_count,
        simulations=args.simulations,
        candidate_step=args.candidate_step / 100.0,
        candidate_padding=args.candidate_padding / 100.0,
        seed=args.seed,
    )


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def sample_competitor_count(config: ScenarioConfig) -> int:
    typical_mode = (config.competitor_typical_low + config.competitor_typical_high) / 2
    count = round(
        random.triangular(
            config.competitor_min,
            config.competitor_typical_high,
            typical_mode,
        )
    )
    return max(config.competitor_min, min(config.competitor_typical_high, count))


def sample_discount(config: ScenarioConfig) -> float:
    sigma = max((config.discount_high - config.discount_low) / 6, 1e-6)
    raw = random.gauss(config.avg_discount, sigma)
    return clamp(raw, config.discount_low, config.discount_high)


def discount_to_bid(control_price: float, discount: float) -> float:
    return control_price * (1 - discount)


def build_candidate_discounts(config: ScenarioConfig) -> list[float]:
    start = max(0.0, config.discount_low - config.candidate_padding)
    end = min(0.9999, config.discount_high + config.candidate_padding)
    candidates: list[float] = []
    current = start
    epsilon = config.candidate_step / 1000

    while current <= end + epsilon:
        candidates.append(min(current, end))
        current += config.candidate_step

    if not candidates or abs(candidates[-1] - end) > epsilon:
        candidates.append(end)

    unique_candidates: list[float] = []
    for candidate in candidates:
        if not unique_candidates or abs(unique_candidates[-1] - candidate) > epsilon:
            unique_candidates.append(candidate)
    return unique_candidates


def compute_base_price(sampled_bids: list[float], a_percent: float) -> float:
    ordered = sorted(sampled_bids)
    trimmed = ordered[1:-1]
    return sum(trimmed) / len(trimmed) * a_percent


def winner_index(bids: list[float], base_price: float) -> int:
    return min(
        range(len(bids)),
        key=lambda idx: (abs(bids[idx] - base_price), bids[idx]),
    )


def sample_bids_for_benchmark(all_bids: list[float], sample_total_count: int) -> list[float]:
    if sample_total_count < 4:
        raise ValueError("抽样总人数至少需要 4，才能去掉一个最高价和一个最低价。")
    if sample_total_count % 2 != 0:
        raise ValueError("抽样总人数必须是偶数，才能分成两组相同数量。")
    if sample_total_count > len(all_bids):
        raise ValueError("抽样总人数不能大于有效报价总人数。")

    indices = list(range(len(all_bids)))
    random.shuffle(indices)
    midpoint = len(indices) // 2
    group_a = indices[:midpoint]
    group_b = indices[midpoint:]
    per_group = sample_total_count // 2

    if len(group_a) < per_group or len(group_b) < per_group:
        raise ValueError("当前有效报价人数不足以按两组等量抽样。")

    sampled_indices = random.sample(group_a, per_group) + random.sample(group_b, per_group)
    return [all_bids[idx] for idx in sampled_indices]


def simulate_candidate(config: ScenarioConfig, my_discount: float) -> dict[str, float]:
    wins = 0
    total_gap = 0.0
    total_base_price = 0.0
    my_bid = discount_to_bid(config.control_price, my_discount)

    for _ in range(config.simulations):
        competitor_count = sample_competitor_count(config)
        competitor_bids = [
            discount_to_bid(config.control_price, sample_discount(config))
            for _ in range(competitor_count)
        ]
        bids = competitor_bids + [my_bid]
        sampled_bids = sample_bids_for_benchmark(bids, config.sample_total_count)
        base_price = compute_base_price(sampled_bids, config.a_percent)
        win_idx = winner_index(bids, base_price)
        my_idx = len(bids) - 1
        if win_idx == my_idx:
            wins += 1
        total_gap += abs(my_bid - base_price)
        total_base_price += base_price

    return {
        "discount": my_discount,
        "bid": my_bid,
        "win_rate": wins / config.simulations,
        "wins": wins,
        "avg_gap": total_gap / config.simulations,
        "avg_base_price": total_base_price / config.simulations,
    }


def run_simulation(config: ScenarioConfig) -> tuple[dict[str, float], list[dict[str, float]], list[float]]:
    if config.seed is not None:
        random.seed(config.seed)

    candidates = build_candidate_discounts(config)
    results = [simulate_candidate(config, discount) for discount in candidates]
    results.sort(key=lambda item: (-item["win_rate"], item["avg_gap"], item["discount"]))
    return results[0], results, candidates


def format_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def format_money(value: float) -> str:
    return f"{value:,.2f}"


def main() -> None:
    config = parse_args()
    best, results, candidates = run_simulation(config)

    print("投标报价模拟器")
    print("=" * 32)
    print(f"Control price: {format_money(config.control_price)}")
    print(
        "Discount scene: "
        f"avg {format_pct(config.avg_discount)}, "
        f"range {format_pct(config.discount_low)} - {format_pct(config.discount_high)}"
    )
    print(
        "Competitor count: "
        f"{config.competitor_min} - {config.competitor_typical_high} "
        f"(typical {config.competitor_typical_low} - {config.competitor_typical_high})"
    )
    print(
        "Benchmark factor A%: "
        f"{config.a_percent * 100:.2f}%"
    )
    print(f"Sample total count: {config.sample_total_count}")
    print(f"Simulations per candidate: {config.simulations}")
    print()
    print("Recommended result")
    print(f"- Discount: {format_pct(best['discount'])}")
    print(f"- Bid price: {format_money(best['bid'])}")
    print(f"- Simulated win rate: {best['win_rate']:.2%}")
    print(f"- Average benchmark price: {format_money(best['avg_base_price'])}")
    print()
    print("Top 10 candidate discounts")
    for idx, item in enumerate(results[:10], start=1):
        print(
            f"{idx:>2}. {format_pct(item['discount'])} | "
            f"bid {format_money(item['bid'])} | "
            f"win rate {item['win_rate']:.2%} | "
            f"avg gap {format_money(item['avg_gap'])}"
        )


class BidSimulatorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("投标报价模拟器")
        self.root.geometry("1180x760")
        self.root.minsize(1040, 680)

        self.inputs: dict[str, tk.StringVar] = {}
        self.status_var = tk.StringVar(value="请先填写参数，然后点击开始模拟。")
        self.summary_var = tk.StringVar(value="结果会显示在这里。")
        self.run_button: ttk.Button | None = None
        self.results_box: tk.Text | None = None

        self._build_ui()

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=0)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(0, weight=1)

        form = ttk.LabelFrame(container, text="输入参数", padding=16)
        form.grid(row=0, column=0, sticky="nsw", padx=(0, 16))

        fields = [
            ("control_price", "最高控制价", "120000000"),
            ("avg_discount", "平均下浮率(%)", "2.25"),
            ("discount_low", "主流下浮下界(%)", "1.8"),
            ("discount_high", "主流下浮上界(%)", "2.6"),
            ("competitor_min", "保底竞争家数", "30"),
            ("competitor_typical_low", "常态竞争下界", "35"),
            ("competitor_typical_high", "常态竞争上界", "48"),
            ("a_percent", "A值(%)", "97"),
            ("sample_total_count", "抽样总人数", "10"),
            ("simulations", "每个报价模拟次数", "5000"),
            ("candidate_step", "搜索步长(百分点)", "0.05"),
            ("candidate_padding", "搜索扩展(百分点)", "0.3"),
            ("seed", "随机种子", "42"),
        ]

        for row, (key, label, default) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=default)
            self.inputs[key] = var
            ttk.Entry(form, textvariable=var, width=18).grid(
                row=row, column=1, sticky="ew", pady=4, padx=(12, 0)
            )

        form.columnconfigure(1, weight=1)

        self.run_button = ttk.Button(form, text="开始模拟", command=self.on_run)
        self.run_button.grid(row=len(fields), column=0, columnspan=2, sticky="ew", pady=(12, 0))

        ttk.Label(
            form,
            text=(
                "规则：先分两组等量随机抽样，合并后剔除1个最高价和1个最低价，"
                "剩余报价取平均，再乘A值得到基准价，最接近基准价者中标。"
            ),
            wraplength=300,
            justify="left",
        ).grid(row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(12, 0))

        output = ttk.Frame(container)
        output.grid(row=0, column=1, sticky="nsew")
        output.columnconfigure(0, weight=1)
        output.rowconfigure(2, weight=1)

        ttk.Label(output, text="模拟结果", font=("PingFang SC", 18, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(output, textvariable=self.summary_var, font=("PingFang SC", 12)).grid(
            row=1, column=0, sticky="w", pady=(8, 4)
        )
        ttk.Label(
            output, text="", wraplength=760, justify="left"
        ).grid(row=2, column=0, sticky="nw", pady=(0, 8))

        self.results_box = tk.Text(output, wrap="word", font=("Menlo", 12))
        self.results_box.grid(row=3, column=0, sticky="nsew")
        self.results_box.insert(
            "1.0",
            "点击“开始模拟”后，这里会显示推荐下浮率、报价金额和前10名候选结果。\n",
        )
        self.results_box.configure(state="disabled")

        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w")
        status_bar.pack(fill="x", side="bottom")

    def _get_config(self) -> ScenarioConfig:
        try:
            seed_raw = self.inputs["seed"].get().strip()
            seed = None if seed_raw == "" else int(seed_raw)
            return ScenarioConfig(
                control_price=float(self.inputs["control_price"].get()),
                avg_discount=float(self.inputs["avg_discount"].get()) / 100.0,
                discount_low=float(self.inputs["discount_low"].get()) / 100.0,
                discount_high=float(self.inputs["discount_high"].get()) / 100.0,
                competitor_min=int(self.inputs["competitor_min"].get()),
                competitor_typical_low=int(self.inputs["competitor_typical_low"].get()),
                competitor_typical_high=int(self.inputs["competitor_typical_high"].get()),
                a_percent=float(self.inputs["a_percent"].get()) / 100.0,
                sample_total_count=int(self.inputs["sample_total_count"].get()),
                simulations=int(self.inputs["simulations"].get()),
                candidate_step=float(self.inputs["candidate_step"].get()) / 100.0,
                candidate_padding=float(self.inputs["candidate_padding"].get()) / 100.0,
                seed=seed,
            )
        except ValueError as exc:
            raise ValueError("请输入合法的数字参数。") from exc

    def _set_results(self, text: str) -> None:
        assert self.results_box is not None
        self.results_box.configure(state="normal")
        self.results_box.delete("1.0", "end")
        self.results_box.insert("1.0", text)
        self.results_box.configure(state="disabled")

    def on_run(self) -> None:
        try:
            config = self._get_config()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        assert self.run_button is not None
        self.run_button.configure(state="disabled")
        self.status_var.set("模拟中，请稍候...")
        self.summary_var.set("正在运行模拟...")
        self._set_results("正在计算，请稍候...\n")

        threading.Thread(target=self._run_in_background, args=(config,), daemon=True).start()

    def _run_in_background(self, config: ScenarioConfig) -> None:
        try:
            best, results, candidates = run_simulation(config)
            lines = [
                f"推荐下浮率: {format_pct(best['discount'])}",
                f"推荐报价: {format_money(best['bid'])}",
                f"模拟中标率: {best['win_rate']:.2%}",
                f"平均基准价: {format_money(best['avg_base_price'])}",
                "",
                "前10名候选报价",
            ]
            for idx, item in enumerate(results[:10], start=1):
                lines.append(
                    f"{idx:>2}. 下浮 {format_pct(item['discount'])} | "
                    f"报价 {format_money(item['bid'])} | "
                    f"中标率 {item['win_rate']:.2%} | "
                    f"平均偏差 {format_money(item['avg_gap'])}"
                )

            summary = (
                f"推荐下浮率 {format_pct(best['discount'])}，"
                f"推荐报价 {format_money(best['bid'])}，"
                f"模拟中标率 {best['win_rate']:.2%}。"
            )

            self.root.after(
                0,
                lambda: self._finish_run(summary, "\n".join(lines)),
            )
        except Exception as exc:  # pragma: no cover - UI fallback
            traceback.print_exc()
            self.root.after(0, lambda: self._fail_run(str(exc)))

    def _finish_run(self, summary: str, text: str) -> None:
        assert self.run_button is not None
        self.run_button.configure(state="normal")
        self.status_var.set("模拟完成。")
        self.summary_var.set(summary)
        self._set_results(text)

    def _fail_run(self, error: str) -> None:
        assert self.run_button is not None
        self.run_button.configure(state="normal")
        self.status_var.set("模拟失败。")
        self.summary_var.set("模拟未完成。")
        self._set_results(f"出错了：{error}\n")
        messagebox.showerror("运行失败", error)


def launch_gui() -> None:
    root = tk.Tk()
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    app = BidSimulatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    import sys

    if "--cli" in sys.argv:
        sys.argv = [arg for arg in sys.argv if arg != "--cli"]
        main()
    else:
        sys.argv = [arg for arg in sys.argv if arg != "--gui"]
        launch_gui()
