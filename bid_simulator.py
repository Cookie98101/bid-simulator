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


class SimulationCancelled(Exception):
    """Raised when the user cancels an in-flight simulation."""


@dataclass
class ScenarioConfig:
    control_price: float
    avg_discount: float = 0.0225
    discount_low: float = 0.018
    discount_high: float = 0.026
    competitor_min: int = 30
    competitor_typical_low: int = 35
    competitor_typical_high: int = 48
    large_sample_total_count: int = 10
    simulations: int = 5000
    candidate_step: float = 0.0005
    candidate_padding: float = 0.0
    seed: int | None = 42


@dataclass(frozen=True)
class OpponentScenario:
    name: str
    avg_shift_points: float = 0.0
    low_shift_points: float = 0.0
    high_shift_points: float = 0.0
    mode: str = "normal"
    spread_scale: float = 1.0


SCENARIO_SHIFT_POINTS = 0.03
SCENARIO_WIDE_POINTS = 0.03
CROWDING_BAND_POINTS = 0.05
A_PERCENT_CHOICES = [0.95, 0.96, 0.97, 0.98, 0.99]


def points_to_decimal(points: float) -> float:
    return points / 100.0


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
        "--large-sample-total-count",
        type=int,
        default=10,
        help="For 11+ bidders, total sampled bidder count across two groups.",
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
        default=0.0,
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
        large_sample_total_count=args.large_sample_total_count,
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


def sample_a_percent() -> float:
    return random.choice(A_PERCENT_CHOICES)


def compute_base_price(sampled_bids: list[float], a_percent: float, trim_extremes: bool) -> float:
    ordered = sorted(sampled_bids)
    if trim_extremes:
        ordered = ordered[1:-1]
    return sum(ordered) / len(ordered) * a_percent


def split_groups(all_bids: list[float]) -> tuple[list[float], list[float]]:
    indices = list(range(len(all_bids)))
    random.shuffle(indices)
    midpoint = len(indices) // 2
    left = [all_bids[idx] for idx in indices[:midpoint]]
    right = [all_bids[idx] for idx in indices[midpoint:]]
    return left, right


def benchmark_sample_rule(total_bidders: int, config: ScenarioConfig) -> tuple[str, int]:
    if total_bidders <= 5:
        return "all", total_bidders
    if total_bidders == 6:
        return "fixed", 3
    if total_bidders == 7:
        return "fixed", 3
    if total_bidders == 8:
        return "fixed", 3
    if total_bidders == 9:
        return "fixed", 4
    if total_bidders == 10:
        return "fixed", 4
    if config.large_sample_total_count < 10 or config.large_sample_total_count % 2 != 0:
        raise ValueError("≥11家的抽样总人数必须是偶数且不小于10。")
    per_group = config.large_sample_total_count // 2
    return "fixed_large", per_group


def sample_bids_for_benchmark(all_bids: list[float], config: ScenarioConfig) -> tuple[list[float], bool]:
    total_bidders = len(all_bids)
    rule, sample_num = benchmark_sample_rule(total_bidders, config)
    if rule == "all":
        return all_bids[:], False

    left_group, right_group = split_groups(all_bids)
    per_group = sample_num

    if rule == "fixed_large":
        per_group = min(per_group, len(left_group), len(right_group))

    if per_group <= 0:
        raise ValueError("抽样人数不足。")

    sampled = random.sample(left_group, min(per_group, len(left_group))) + random.sample(
        right_group, min(per_group, len(right_group))
    )
    return sampled, True


def build_opponent_scenarios(config: ScenarioConfig) -> list[OpponentScenario]:
    shift = points_to_decimal(SCENARIO_SHIFT_POINTS)
    wide = points_to_decimal(SCENARIO_WIDE_POINTS)
    return [
        OpponentScenario(name="A", mode="normal"),
        OpponentScenario(name="B", avg_shift_points=-shift, low_shift_points=-shift, high_shift_points=-shift, mode="normal"),
        OpponentScenario(name="C", avg_shift_points=shift, low_shift_points=shift, high_shift_points=shift, mode="normal"),
        OpponentScenario(
            name="D",
            low_shift_points=-wide,
            high_shift_points=wide,
            mode="normal",
            spread_scale=1.4,
        ),
        OpponentScenario(name="E", mode="upper_cluster", spread_scale=1.0),
    ]


def sample_discount_for_scenario(config: ScenarioConfig, scenario: OpponentScenario) -> float:
    low = clamp(config.discount_low + scenario.low_shift_points, 0.0, 0.9999)
    high = clamp(config.discount_high + scenario.high_shift_points, low + 1e-6, 0.9999)
    avg = clamp(config.avg_discount + scenario.avg_shift_points, low, high)
    span = max(high - low, 1e-6)

    if scenario.mode == "upper_cluster":
        raw = low + span * random.betavariate(8.0, 2.0)
    else:
        sigma = max((span / 6.0) * scenario.spread_scale, 1e-6)
        raw = random.gauss(avg, sigma)
    return clamp(raw, low, high)


def simulate_candidate_under_scenario(
    config: ScenarioConfig,
    my_discount: float,
    scenario: OpponentScenario,
    cancel_event: threading.Event | None = None,
) -> dict[str, float]:
    wins = 0.0
    total_gap = 0.0
    total_base_price = 0.0
    total_crowding = 0.0
    my_bid = discount_to_bid(config.control_price, my_discount)

    for idx in range(config.simulations):
        if cancel_event is not None and idx % 50 == 0 and cancel_event.is_set():
            raise SimulationCancelled("用户已停止本次模拟。")
        competitor_count = sample_competitor_count(config)
        competitor_bids = [
            discount_to_bid(config.control_price, sample_discount_for_scenario(config, scenario))
            for _ in range(competitor_count)
        ]
        bids = competitor_bids + [my_bid]
        sampled_bids, trim_extremes = sample_bids_for_benchmark(bids, config)
        a_percent = sample_a_percent()
        base_price = compute_base_price(sampled_bids, a_percent, trim_extremes)
        gaps = [abs(bid - base_price) for bid in bids]
        best_gap = min(gaps)
        tied_winners = [idx for idx, gap in enumerate(gaps) if abs(gap - best_gap) <= 1e-9]
        my_idx = len(bids) - 1
        if my_idx in tied_winners:
            wins += 1.0 / len(tied_winners)
        total_gap += abs(my_bid - base_price)
        total_base_price += base_price
        crowd_band = max(config.candidate_step * 1.5, points_to_decimal(CROWDING_BAND_POINTS))
        total_crowding += sum(1 for bid in competitor_bids if abs(bid - my_bid) <= crowd_band)

    return {
        "discount": my_discount,
        "bid": my_bid,
        "win_rate": wins / config.simulations,
        "avg_gap": total_gap / config.simulations,
        "avg_base_price": total_base_price / config.simulations,
        "crowding": total_crowding / config.simulations,
    }


def simulate_candidate(
    config: ScenarioConfig,
    my_discount: float,
    cancel_event: threading.Event | None = None,
) -> dict[str, float]:
    scenarios = build_opponent_scenarios(config)
    scenario_results = [
        simulate_candidate_under_scenario(config, my_discount, scenario, cancel_event)
        for scenario in scenarios
    ]
    scenario_win_rates = {scenario.name: result["win_rate"] for scenario, result in zip(scenarios, scenario_results)}
    avg_win_rate = sum(item["win_rate"] for item in scenario_results) / len(scenario_results)
    worst_win_rate = min(item["win_rate"] for item in scenario_results)
    win_rate_sensitivity = max(item["win_rate"] for item in scenario_results) - worst_win_rate
    avg_gap = sum(item["avg_gap"] for item in scenario_results) / len(scenario_results)
    avg_crowding = sum(item["crowding"] for item in scenario_results) / len(scenario_results)
    avg_base_price = sum(item["avg_base_price"] for item in scenario_results) / len(scenario_results)
    robust_score = avg_win_rate - 0.5 * win_rate_sensitivity - 0.02 * avg_crowding

    return {
        "discount": my_discount,
        "bid": discount_to_bid(config.control_price, my_discount),
        "win_rate": avg_win_rate,
        "avg_win_rate": avg_win_rate,
        "worst_win_rate": worst_win_rate,
        "win_rate_sensitivity": win_rate_sensitivity,
        "avg_gap": avg_gap,
        "avg_base_price": avg_base_price,
        "avg_crowding": avg_crowding,
        "robust_score": robust_score,
        "scenario_win_rates": scenario_win_rates,
    }


def pick_best_by_scenario(results: list[dict[str, float]], scenario_names: list[str]) -> dict[str, dict[str, float]]:
    best_by_scenario: dict[str, dict[str, float]] = {}
    for scenario_name in scenario_names:
        ranked = sorted(
            results,
            key=lambda item: (
                -item["scenario_win_rates"][scenario_name],
                item["avg_gap"],
                item["discount"],
            ),
        )
        best_by_scenario[scenario_name] = ranked[0]
    return best_by_scenario


def run_simulation(
    config: ScenarioConfig,
    cancel_event: threading.Event | None = None,
) -> tuple[dict[str, float], list[dict[str, float]], list[float], dict[str, dict[str, float]]]:
    if config.seed is not None:
        random.seed(config.seed)

    candidates = build_candidate_discounts(config)
    results: list[dict[str, float]] = []
    for discount in candidates:
        if cancel_event is not None and cancel_event.is_set():
            raise SimulationCancelled("用户已停止本次模拟。")
        results.append(simulate_candidate(config, discount, cancel_event))
    results.sort(
        key=lambda item: (
            -item["avg_win_rate"],
            -item["worst_win_rate"],
            item["win_rate_sensitivity"],
            item["avg_crowding"],
            item["avg_gap"],
            item["discount"],
        )
    )
    scenario_names = [scenario.name for scenario in build_opponent_scenarios(config)]
    scenario_best = pick_best_by_scenario(results, scenario_names)
    return results[0], results, candidates, scenario_best


def format_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def format_money(value: float) -> str:
    return f"{value:,.2f}"


def main() -> None:
    config = parse_args()
    best, results, candidates, scenario_best = run_simulation(config)

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
        "random from 95%/96%/97%/98%/99%"
    )
    print(f"Sample total count (11+): {config.large_sample_total_count}")
    print(f"Simulations per candidate: {config.simulations}")
    print()
    print("Overall robust recommendation")
    print(f"- Discount: {format_pct(best['discount'])}")
    print(f"- Bid price: {format_money(best['bid'])}")
    print(f"- Average win rate: {best['avg_win_rate']:.2%}")
    print(f"- Worst win rate: {best['worst_win_rate']:.2%}")
    print(f"- Average benchmark price: {format_money(best['avg_base_price'])}")
    print(f"- Scenarios: {' '.join(f'{k}:{v:.2%}' for k, v in best['scenario_win_rates'].items())}")
    print()
    print("Best quote for each scenario")
    for name, item in scenario_best.items():
        print(
            f"- {name}: {format_pct(item['discount'])} | "
            f"bid {format_money(item['bid'])} | "
            f"win rate {item['scenario_win_rates'][name]:.2%}"
        )
    print()
    print("Top 10 candidate discounts (robust ranking)")
    for idx, item in enumerate(results[:10], start=1):
        print(
            f"{idx:>2}. {format_pct(item['discount'])} | "
            f"avg {item['avg_win_rate']:.2%} | "
            f"worst {item['worst_win_rate']:.2%} | "
            f"gap {format_money(item['avg_gap'])}"
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
        self.stop_button: ttk.Button | None = None
        self.results_box: tk.Text | None = None
        self.results_scrollbar: ttk.Scrollbar | None = None
        self.cancel_event = threading.Event()

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
            ("large_sample_total_count", "≥11家抽样总人数", "10"),
            ("simulations", "每个报价模拟次数", "5000"),
            ("candidate_step", "搜索步长(百分点)", "0.05"),
            ("candidate_padding", "搜索扩展(百分点)", "0"),
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

        button_row = ttk.Frame(form)
        button_row.grid(row=len(fields), column=0, columnspan=2, sticky="ew", pady=(12, 0))
        button_row.columnconfigure(0, weight=1)
        button_row.columnconfigure(1, weight=1)

        self.run_button = ttk.Button(button_row, text="开始模拟", command=self.on_run)
        self.run_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.stop_button = ttk.Button(
            button_row, text="停止模拟", command=self.on_stop, state="disabled"
        )
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        ttk.Separator(form, orient="horizontal").grid(
            row=len(fields) + 1, column=0, columnspan=2, sticky="ew", pady=(12, 8)
        )

        ttk.Label(
            form,
            text=(
                "规则：≤5家时全体直接算术平均乘随机A%；6-10家按表抽样后去最高最低再平均乘随机A%；"
                "≥11家按两组等量抽样后去最高最低再平均乘随机A%。\n"
                "场景A：按我方原区间模拟对手报价。\n"
                "场景B：对手整体比我方判断低0.03个百分点。\n"
                "场景C：对手整体比我方判断高0.03个百分点。\n"
                "场景D：对手报价区间更宽，波动更大。\n"
                "场景E：对手大量集中在下浮上边界附近。"
            ),
            wraplength=300,
            justify="left",
        ).grid(row=len(fields) + 2, column=0, columnspan=2, sticky="w", pady=(0, 0))

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

        results_frame = ttk.Frame(output)
        results_frame.grid(row=3, column=0, sticky="nsew")
        results_frame.columnconfigure(0, weight=1)
        results_frame.rowconfigure(0, weight=1)

        self.results_box = tk.Text(results_frame, wrap="word", font=("Menlo", 12))
        self.results_box.grid(row=0, column=0, sticky="nsew")
        self.results_scrollbar = ttk.Scrollbar(
            results_frame, orient="vertical", command=self.results_box.yview
        )
        self.results_scrollbar.grid(row=0, column=1, sticky="ns")
        self.results_box.configure(yscrollcommand=self.results_scrollbar.set)
        self.results_box.insert(
            "1.0",
            "点击“开始模拟”后，这里会显示总体稳健推荐、各场景单独最优报价和前10名稳健候选结果。\n",
        )
        self.results_box.bind("<Enter>", self._bind_results_mousewheel)
        self.results_box.bind("<Leave>", self._unbind_results_mousewheel)
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
                large_sample_total_count=int(self.inputs["large_sample_total_count"].get()),
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

    def _bind_results_mousewheel(self, _event: tk.Event) -> None:
        assert self.results_box is not None
        self.results_box.bind_all("<MouseWheel>", self._on_results_mousewheel)
        self.results_box.bind_all("<Button-4>", self._on_results_mousewheel)
        self.results_box.bind_all("<Button-5>", self._on_results_mousewheel)

    def _unbind_results_mousewheel(self, _event: tk.Event) -> None:
        assert self.results_box is not None
        self.results_box.unbind_all("<MouseWheel>")
        self.results_box.unbind_all("<Button-4>")
        self.results_box.unbind_all("<Button-5>")

    def _on_results_mousewheel(self, event: tk.Event) -> str:
        assert self.results_box is not None
        if getattr(event, "num", None) == 4:
            self.results_box.yview_scroll(-1, "units")
            return "break"
        if getattr(event, "num", None) == 5:
            self.results_box.yview_scroll(1, "units")
            return "break"

        delta = getattr(event, "delta", 0)
        if delta == 0:
            return "break"
        step = -1 if delta > 0 else 1
        self.results_box.yview_scroll(step, "units")
        return "break"

    def on_run(self) -> None:
        try:
            config = self._get_config()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        assert self.run_button is not None
        assert self.stop_button is not None
        self.cancel_event = threading.Event()
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("模拟中，请稍候。可点击“停止模拟”中断。")
        self.summary_var.set("正在运行模拟...")
        self._set_results("正在计算，请稍候...\n")

        threading.Thread(target=self._run_in_background, args=(config,), daemon=True).start()

    def on_stop(self) -> None:
        assert self.stop_button is not None
        self.cancel_event.set()
        self.stop_button.configure(state="disabled")
        self.status_var.set("正在停止模拟，请稍候...")
        self.summary_var.set("正在停止...")

    def _run_in_background(self, config: ScenarioConfig) -> None:
        try:
            best, results, candidates, scenario_best = run_simulation(config, self.cancel_event)
            scenario_text = " | ".join(
                f"{k}:{v:.2%}" for k, v in best["scenario_win_rates"].items()
            )
            lines = [
                "【总体稳健推荐】",
                f"下浮率: {format_pct(best['discount'])}",
                f"报价: {format_money(best['bid'])}",
                f"平均中标率: {best['avg_win_rate']:.2%}",
                f"最差场景中标率: {best['worst_win_rate']:.2%}",
                f"平均基准价: {format_money(best['avg_base_price'])}",
                f"场景结果: {scenario_text}",
                "",
                "【各场景单独最优报价】",
            ]
            for name, item in scenario_best.items():
                lines.append(
                    f"场景{name}: 下浮 {format_pct(item['discount'])} | "
                    f"报价 {format_money(item['bid'])} | "
                    f"中标率 {item['scenario_win_rates'][name]:.2%}"
                )
            lines.extend(
                [
                    "",
                    "【前10名稳健候选报价】",
                ]
            )
            for idx, item in enumerate(results[:10], start=1):
                lines.append(
                    f"{idx:>2}. 下浮 {format_pct(item['discount'])} | "
                    f"平均 {item['avg_win_rate']:.2%} | "
                    f"最差 {item['worst_win_rate']:.2%} | "
                    f"偏差 {format_money(item['avg_gap'])}"
                )

            summary = (
                f"总体稳健推荐下浮率 {format_pct(best['discount'])}，"
                f"报价 {format_money(best['bid'])}，"
                f"平均中标率 {best['avg_win_rate']:.2%}。"
            )

            self.root.after(
                0,
                lambda: self._finish_run(summary, "\n".join(lines)),
            )
        except SimulationCancelled as exc:
            self.root.after(0, lambda: self._cancel_run(str(exc)))
        except Exception as exc:  # pragma: no cover - UI fallback
            traceback.print_exc()
            self.root.after(0, lambda: self._fail_run(str(exc)))

    def _finish_run(self, summary: str, text: str) -> None:
        assert self.run_button is not None
        assert self.stop_button is not None
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("模拟完成。")
        self.summary_var.set(summary)
        self._set_results(text)

    def _cancel_run(self, message: str) -> None:
        assert self.run_button is not None
        assert self.stop_button is not None
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("模拟已停止。")
        self.summary_var.set("本次模拟已取消。")
        self._set_results(f"{message}\n")

    def _fail_run(self, error: str) -> None:
        assert self.run_button is not None
        assert self.stop_button is not None
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
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
