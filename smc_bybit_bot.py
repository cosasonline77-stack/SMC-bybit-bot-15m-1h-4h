#!/usr/bin/env python3
"""
SMC Trading Bot — Bybit Futuros Perpetuos (Linear USDT)
3 Timeframes en paralelo: 15m | 1h | 4h
Cada TF envía señales a su propio topic de Telegram

Requiere: pip install ccxt pandas numpy requests
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
import pandas as pd
import numpy as np
import ccxt
import requests
from dataclasses import dataclass

# ╔══════════════════════════════════════════════════╗
#   CONFIGURACIÓN — edita solo esta sección
# ╚══════════════════════════════════════════════════╝

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# Topic ID por timeframe
TOPIC_15M = 268
TOPIC_1H  = 395
TOPIC_4H  = 401

TZ_OFFSET = -6
TZ        = timezone(timedelta(hours=TZ_OFFSET))

TOP_N_SYMBOLS     = 100
SYMBOLS_REFRESH_H = 6
MIN_VOLUME_USDT   = 1_000_000

OB_LOOKBACK    = 60
FVG_MIN_SIZE   = 0.08
CONFLUENCE_MIN = 2
EMA_FAST       = 50
EMA_SLOW       = 200

# Configuración individual por timeframe
TF_CONFIG = {
    "15m": {
        "trend_tf":         "1h",
        "check_interval":   60,
        "rr_min":           2.0,
        "tp_rr":            [2.0, 3.5, 5.0],
        "tp_pct":           [33,  33,  34],
        "alert_cooldown_h": 4,
        "risk_pct":         1.0,
        "max_leverage":     25,
        "max_sl_pct":       3.0,
        "topic":            268,
        "label":            "15M",
    },
    "1h": {
        "trend_tf":         "4h",
        "check_interval":   300,
        "rr_min":           3.0,
        "tp_rr":            [3.0, 5.0, 8.0],
        "tp_pct":           [25,  35,  40],
        "alert_cooldown_h": 8,
        "risk_pct":         1.0,
        "max_leverage":     25,
        "max_sl_pct":       5.0,
        "topic":            395,
        "label":            "1H",
    },
    "4h": {
        "trend_tf":         "1d",
        "check_interval":   900,
        "rr_min":           3.0,
        "tp_rr":            [3.0, 5.0, 8.0],
        "tp_pct":           [25,  35,  40],
        "alert_cooldown_h": 24,
        "risk_pct":         1.0,
        "max_leverage":     25,
        "max_sl_pct":       8.0,
        "topic":            401,
        "label":            "4H",
    },
}

BYBIT_API_KEY    = ""
BYBIT_API_SECRET = ""

# ╚══════════════════════════════════════════════════╝

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("SMC-Bot")


@dataclass
class Signal:
    symbol: str;    direction: str
    entry: float;   stop_loss: float
    tp1: float;     tp2: float;   tp3: float
    rr1: float;     rr2: float;   rr3: float
    sl_pct: float;  lev_low: int;  lev_high: int
    reason: str;    timeframe: str; timestamp: str
    funding_rate:         float = 0.0
    open_interest_change: str   = ""

@dataclass
class MarketContext:
    funding_rate: float = 0.0;  mark_price: float = 0.0
    open_interest: float = 0.0; oi_prev: float = 0.0


def build_exchange():
    p = {"enableRateLimit": True,
         "options": {"defaultType": "linear", "adjustForTimeDifference": True}}
    if BYBIT_API_KEY:
        p["apiKey"] = BYBIT_API_KEY
        p["secret"] = BYBIT_API_SECRET
    return ccxt.bybit(p)


def fetch_top_symbols(exchange, top_n=TOP_N_SYMBOLS):
    log.info("Cargando pares desde Bybit...")
    try:
        markets = exchange.load_markets()
        perps   = [m for m in markets.values()
                   if m.get("linear") and m.get("active") and m.get("swap")
                   and m.get("quote") == "USDT" and m.get("settle") == "USDT"]
        if not perps: return _fallback()
        syms = [m["symbol"] for m in perps]
        try:    tickers = exchange.fetch_tickers(syms)
        except:
            tickers = {}
            for s in syms[:200]:
                try: tickers[s] = exchange.fetch_ticker(s); time.sleep(0.05)
                except: pass
        ranked = []
        for s, t in tickers.items():
            v = t.get("quoteVolume") or (t.get("baseVolume", 0) * t.get("last", 0))
            if v >= MIN_VOLUME_USDT: ranked.append((s, v))
        ranked.sort(key=lambda x: x[1], reverse=True)
        result = [s for s, _ in ranked[:top_n]]
        log.info(f"{len(result)} pares. Top 3: {', '.join(result[:3])}")
        return result or _fallback()
    except Exception as e:
        log.error(f"Error pares: {e}"); return _fallback()


def _fallback():
    log.warning("Usando lista de respaldo.")
    return ["BTC/USDT:USDT","ETH/USDT:USDT","SOL/USDT:USDT","BNB/USDT:USDT",
            "XRP/USDT:USDT","DOGE/USDT:USDT","ADA/USDT:USDT","AVAX/USDT:USDT",
            "LINK/USDT:USDT","OP/USDT:USDT","ARB/USDT:USDT","MATIC/USDT:USDT",
            "DOT/USDT:USDT","LTC/USDT:USDT","ATOM/USDT:USDT","UNI/USDT:USDT",
            "SUI/USDT:USDT","TRX/USDT:USDT","FIL/USDT:USDT","INJ/USDT:USDT"]


def send_telegram(msg: str, topic_id: Optional[int] = None) -> bool:
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    if topic_id is not None:
        body["message_thread_id"] = topic_id
    try:
        r = requests.post(url, json=body, timeout=10)
        if r.status_code != 200: log.warning(f"TG {r.status_code}: {r.text[:150]}")
        return r.status_code == 200
    except Exception as e:
        log.error(f"TG error: {e}"); return False


def _now():
    return datetime.now(TZ).strftime(f"%Y-%m-%d %H:%M (UTC{TZ_OFFSET:+d})")

def _pf(p):
    if p >= 1000: return f"{p:,.2f}"
    if p >= 1:    return f"{p:.4f}"
    return f"{p:.6f}"

LEV_MIN = 3    # apalancamiento mínimo para trader retail
LEV_MAX = 25   # apalancamiento máximo absoluto

def calc_leverage_range(entry, sl, risk_pct, max_lev):
    """
    Devuelve (lev_min, lev_max) como rango recomendado.

    lev_conservador = RISK% / SL%          → entrada conservadora
    lev_agresivo    = lev_conservador * 2.5 → límite agresivo razonable

    Ambos valores se clampean entre LEV_MIN y min(max_lev, LEV_MAX).
    """
    sl_pct = abs(entry - sl) / entry * 100
    if sl_pct <= 0:
        return (LEV_MIN, LEV_MIN)

    techo       = min(max_lev, LEV_MAX)
    lev_base    = risk_pct / sl_pct                          # ej: 1% / 0.5% = 2x
    lev_low     = max(LEV_MIN, min(techo, int(lev_base)))    # mínimo siempre 3x
    lev_high    = max(LEV_MIN, min(techo, int(lev_base * 2.5)))  # hasta 2.5x el base

    # Asegurar que lev_high > lev_low para que el rango tenga sentido
    if lev_high <= lev_low:
        lev_high = min(techo, lev_low + 2)

    return (lev_low, lev_high)


def lev_label(lev_low, lev_high):
    """Etiqueta de riesgo basada en el techo del rango."""
    if lev_high >= 20: return "⚠️ Alto riesgo — gestión estricta"
    if lev_high >= 15: return "🟡 Moderado-alto — SL obligatorio"
    if lev_high >= 8:  return "🟢 Moderado — recomendado retail"
    return "✅ Conservador — ideal para comenzar"


def format_signal(s: Signal, cfg: dict) -> str:
    e  = "🟢" if s.direction == "LONG" else "🔴"
    a  = "📈" if s.direction == "LONG" else "📉"
    d  = s.symbol.replace(":USDT", " PERP")
    tp = cfg["tp_pct"]
    fl = ""
    if s.funding_rate:
        fr = s.funding_rate * 100
        fl = f"{'🔺' if fr>0 else '🔻'} <b>Funding:</b> <code>{fr:+.4f}%</code>\n"
    oi = f"📊 <b>OI:</b> {s.open_interest_change}\n" if s.open_interest_change else ""
    cf = "\n".join(f"  • {r}" for r in s.reason.split(" | "))
    return (
        f"{e} <b>SMC {s.timeframe} — {s.direction}</b> {a}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏦 Bybit Futures | <b>{d}</b>\n"
        f"⏱  TF: <b>{s.timeframe}</b> | Tendencia: <b>{cfg['trend_tf']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Entrada:</b>   <code>{_pf(s.entry)}</code>\n"
        f"🛑 <b>Stop Loss:</b> <code>{_pf(s.stop_loss)}</code>  <i>(-{s.sl_pct:.2f}%)</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>TP1 {tp[0]}%:</b> <code>{_pf(s.tp1)}</code>  R/R <b>{s.rr1:.1f}x</b>\n"
        f"🎯 <b>TP2 {tp[1]}%:</b> <code>{_pf(s.tp2)}</code>  R/R <b>{s.rr2:.1f}x</b>\n"
        f"🎯 <b>TP3 {tp[2]}%:</b> <code>{_pf(s.tp3)}</code>  R/R <b>{s.rr3:.1f}x</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ <b>Apalancamiento rec.:</b> <code>{s.lev_low}x – {s.lev_high}x</code>  {lev_label(s.lev_low, s.lev_high)}\n"
        f"💡 <i>Conservador: {s.lev_low}x | Agresivo: {s.lev_high}x | Máx absoluto: {LEV_MAX}x</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{fl}{oi}"
        f"📋 <b>Confluencias:</b>\n{cf}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {s.timestamp}\n"
        f"⚠️ <i>Solo informativo. No es asesoría financiera.</i>"
    )


class SMCAnalyzer:
    def __init__(self, df):
        self.df = df.copy().reset_index(drop=True)
        d = self.df
        d["body"]     = abs(d["close"] - d["open"])
        d["rng"]      = d["high"] - d["low"]
        d["upper"]    = d["high"] - d[["close","open"]].max(axis=1)
        d["lower"]    = d[["close","open"]].min(axis=1) - d["low"]
        d["is_bull"]  = d["close"] > d["open"]
        d["is_bear"]  = d["close"] < d["open"]
        d["body_pct"] = d["body"] / d["rng"].replace(0, np.nan)

    def atr(self, p=14):
        d=self.df; hi,lo,cl=d["high"],d["low"],d["close"].shift(1)
        return pd.concat([hi-lo,(hi-cl).abs(),(lo-cl).abs()],axis=1).max(axis=1).rolling(p).mean().iloc[-1]

    def ema(self, p): return self.df["close"].ewm(span=p, adjust=False).mean()

    def trend_bias(self):
        c=self.df["close"].iloc[-1]; e50=self.ema(EMA_FAST).iloc[-1]; e200=self.ema(EMA_SLOW).iloc[-1]
        if c>e50 and e50>e200: return "bullish"
        if c<e50 and e50<e200: return "bearish"
        return "neutral"

    def swing_highs(self,n=5):
        h=self.df["high"]; return h==h.rolling(2*n+1,center=True).max()
    def swing_lows(self,n=5):
        l=self.df["low"];  return l==l.rolling(2*n+1,center=True).min()

    def market_structure(self):
        d=self.df
        hs=d.loc[self.swing_highs(),"high"].tail(3).values
        ls=d.loc[self.swing_lows(), "low"].tail(3).values
        if len(hs)<2 or len(ls)<2: return "ranging"
        if hs[-1]>hs[-2] and ls[-1]>ls[-2]: return "bullish"
        if hs[-1]<hs[-2] and ls[-1]<ls[-2]: return "bearish"
        return "ranging"

    def choch_bullish(self):
        hs=self.df.loc[self.swing_highs(),"high"].tail(4).values
        return len(hs)>=3 and hs[-2]>hs[-1] and self.df["close"].iloc[-1]>hs[-1]

    def choch_bearish(self):
        ls=self.df.loc[self.swing_lows(),"low"].tail(4).values
        return len(ls)>=3 and ls[-2]<ls[-1] and self.df["close"].iloc[-1]<ls[-1]

    def find_bullish_ob(self):
        d=self.df; ab=d["body"].rolling(20).mean()
        for i in range(len(d)-2,max(len(d)-OB_LOOKBACK,1),-1):
            if d.at[i,"is_bear"] and i+1<len(d):
                if d.at[i+1,"is_bull"] and d.at[i+1,"body"]>ab.iloc[i]*1.4:
                    return {"high":d.at[i,"high"],"low":d.at[i,"low"]}
        return None

    def find_bearish_ob(self):
        d=self.df; ab=d["body"].rolling(20).mean()
        for i in range(len(d)-2,max(len(d)-OB_LOOKBACK,1),-1):
            if d.at[i,"is_bull"] and i+1<len(d):
                if d.at[i+1,"is_bear"] and d.at[i+1,"body"]>ab.iloc[i]*1.4:
                    return {"high":d.at[i,"high"],"low":d.at[i,"low"]}
        return None

    def find_bullish_fvg(self):
        d=self.df
        for i in range(len(d)-3,max(len(d)-OB_LOOKBACK,0),-1):
            lo,hi=d.at[i+2,"low"],d.at[i,"high"]
            if lo>hi and (lo-hi)/hi*100>=FVG_MIN_SIZE: return {"high":lo,"low":hi}
        return None

    def find_bearish_fvg(self):
        d=self.df
        for i in range(len(d)-3,max(len(d)-OB_LOOKBACK,0),-1):
            hi,lo=d.at[i+2,"high"],d.at[i,"low"]
            if hi<lo and (lo-hi)/lo*100>=FVG_MIN_SIZE: return {"high":lo,"low":hi}
        return None

    def liq_sweep_down(self):
        d=self.df
        if len(d)<12: return False
        pl=d["low"].iloc[-12:-3].min()
        return d["low"].iloc[-2]<pl and d["close"].iloc[-1]>pl and d["lower"].iloc[-2]>d["body"].iloc[-2]*1.2

    def liq_sweep_up(self):
        d=self.df
        if len(d)<12: return False
        ph=d["high"].iloc[-12:-3].max()
        return d["high"].iloc[-2]>ph and d["close"].iloc[-1]<ph and d["upper"].iloc[-2]>d["body"].iloc[-2]*1.2

    def in_discount(self):
        d=self.df; eq=(d["high"].tail(OB_LOOKBACK).max()+d["low"].tail(OB_LOOKBACK).min())/2
        return d["close"].iloc[-1]<eq

    def in_premium(self):
        d=self.df; eq=(d["high"].tail(OB_LOOKBACK).max()+d["low"].tail(OB_LOOKBACK).min())/2
        return d["close"].iloc[-1]>eq

    def generate_signal(self, symbol, tf, cfg, ctx=None, trend_bias="neutral"):
        if trend_bias=="neutral": return None
        al=(trend_bias=="bullish"); ash=(trend_bias=="bearish")
        d=self.df; close=d["close"].iloc[-1]; atr=self.atr()
        st=self.market_structure()
        bob=self.find_bullish_ob(); bab=self.find_bearish_ob()
        bfv=self.find_bullish_fvg(); bafv=self.find_bearish_fvg()

        lr=[]
        if al:
            lr.append(f"📊 Tendencia Alcista (EMA{EMA_FAST}>EMA{EMA_SLOW})")
            if st=="bullish": lr.append("✅ Estructura Alcista (HH+HL)")
            if self.choch_bullish(): lr.append("🔀 CHoCH Alcista detectado")
            if bob and bob["low"]*0.998<=close<=bob["high"]*1.003:
                lr.append(f"🧱 OB Alcista [{_pf(bob['low'])} – {_pf(bob['high'])}]")
            if bfv and bfv["low"]*0.999<=close<=bfv["high"]*1.001:
                lr.append(f"⬜ FVG Alcista [{_pf(bfv['low'])} – {_pf(bfv['high'])}]")
            if self.liq_sweep_down(): lr.append("💧 Liquidity Sweep Bajista → Reversión")
            if self.in_discount():    lr.append("📉 Precio en Zona Descuento")
            if ctx and ctx.funding_rate<-0.0005:
                lr.append(f"💰 Funding negativo ({ctx.funding_rate*100:+.4f}%)")

        sr=[]
        if ash:
            sr.append(f"📊 Tendencia Bajista (EMA{EMA_FAST}<EMA{EMA_SLOW})")
            if st=="bearish": sr.append("✅ Estructura Bajista (LL+LH)")
            if self.choch_bearish(): sr.append("🔀 CHoCH Bajista detectado")
            if bab and bab["low"]*0.998<=close<=bab["high"]*1.003:
                sr.append(f"🧱 OB Bajista [{_pf(bab['low'])} – {_pf(bab['high'])}]")
            if bafv and bafv["low"]*0.999<=close<=bafv["high"]*1.001:
                sr.append(f"⬜ FVG Bajista [{_pf(bafv['low'])} – {_pf(bafv['high'])}]")
            if self.liq_sweep_up():  sr.append("💧 Liquidity Sweep Alcista → Reversión")
            if self.in_premium():    sr.append("📈 Precio en Zona Premium")
            if ctx and ctx.funding_rate>0.0005:
                sr.append(f"💰 Funding positivo ({ctx.funding_rate*100:+.4f}%)")

        oi_txt=""
        if ctx and ctx.oi_prev>0:
            oi_txt=f"{(ctx.open_interest-ctx.oi_prev)/ctx.oi_prev*100:+.2f}% vs escaneo anterior"

        def make(direction, reasons):
            entry=close; tp_rr=cfg["tp_rr"]
            if direction=="LONG":
                sl=(bob["low"]-atr*0.3) if bob else (d["low"].tail(15).min()-atr*0.2)
                sl=min(sl, entry-atr*0.5); risk=entry-sl
                if risk<=0: return None
                tp1=entry+risk*tp_rr[0]; tp2=entry+risk*tp_rr[1]; tp3=entry+risk*tp_rr[2]
            else:
                sl=(bab["high"]+atr*0.3) if bab else (d["high"].tail(15).max()+atr*0.2)
                sl=max(sl, entry+atr*0.5); risk=sl-entry
                if risk<=0: return None
                tp1=entry-risk*tp_rr[0]; tp2=entry-risk*tp_rr[1]; tp3=entry-risk*tp_rr[2]

            rr1=abs(tp1-entry)/risk; rr2=abs(tp2-entry)/risk; rr3=abs(tp3-entry)/risk
            if rr1<cfg["rr_min"]: return None
            sl_pct=abs(entry-sl)/entry*100
            if sl_pct>cfg["max_sl_pct"]: return None
            lev_low, lev_high = calc_leverage_range(entry, sl, cfg["risk_pct"], cfg["max_leverage"])
            return Signal(
                symbol=symbol, direction=direction,
                entry=round(entry,8), stop_loss=round(sl,8),
                tp1=round(tp1,8), tp2=round(tp2,8), tp3=round(tp3,8),
                rr1=round(rr1,1), rr2=round(rr2,1), rr3=round(rr3,1),
                sl_pct=round(sl_pct,2), lev_low=lev_low, lev_high=lev_high,
                reason=" | ".join(reasons), timeframe=tf, timestamp=_now(),
                funding_rate=ctx.funding_rate if ctx else 0.0,
                open_interest_change=oi_txt,
            )

        if al  and len(lr)>=CONFLUENCE_MIN and len(lr)>=len(sr): return make("LONG",  lr)
        if ash and len(sr)>=CONFLUENCE_MIN:                       return make("SHORT", sr)
        return None


class BybitSMCBot:
    def __init__(self):
        self.exchange     = build_exchange()
        self.symbols:list = []
        self.last_refresh = 0.0
        self.last_signals: dict = {}
        self.oi_cache:     dict = {}

    def refresh_if_needed(self, force=False):
        now=time.time()
        if not force and now-self.last_refresh < SYMBOLS_REFRESH_H*3600: return
        new=fetch_top_symbols(self.exchange); changed=set(new)!=set(self.symbols)
        self.symbols=new; self.last_refresh=now
        if changed or force:
            muestra="\n".join(f"  {i+1}. {s.replace(':USDT',' PERP')}"
                              for i,s in enumerate(new[:20]))
            resto=f"\n  ... y {len(new)-20} más" if len(new)>20 else ""
            send_telegram(
                f"📋 <b>Pares actualizados</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>{len(new)} pares USDT PERP</b> — Top por volumen 24h\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n{muestra}{resto}",
                topic_id=TOPIC_15M,
            )

    def fetch_ohlcv(self, symbol, tf, limit=220):
        try:
            raw=self.exchange.fetch_ohlcv(symbol,tf,limit=limit)
            if not raw: return None
            df=pd.DataFrame(raw,columns=["timestamp","open","high","low","close","volume"])
            df["timestamp"]=pd.to_datetime(df["timestamp"],unit="ms"); return df
        except Exception as e: log.warning(f"OHLCV {symbol} {tf}: {e}"); return None

    def fetch_ctx(self, symbol, tf):
        ctx=MarketContext()
        try: ctx.mark_price=self.exchange.fetch_ticker(symbol).get("last",0.0) or 0.0
        except: pass
        try: ctx.funding_rate=self.exchange.fetch_funding_rate(symbol).get("fundingRate",0.0) or 0.0
        except: pass
        try:
            key=f"{symbol}_{tf}"
            oi=self.exchange.fetch_open_interest(symbol).get("openInterestAmount",0.0) or 0.0
            ctx.open_interest=oi; ctx.oi_prev=self.oi_cache.get(key,oi)
            self.oi_cache[key]=oi
        except: pass
        return ctx

    def already_alerted(self, sig, cfg):
        key=f"{sig.symbol}_{sig.direction}_{sig.timeframe}"; now=time.time()
        if now-self.last_signals.get(key,0.0) < cfg["alert_cooldown_h"]*3600: return True
        self.last_signals[key]=now; return False

    async def scan_symbol(self, symbol, tf, cfg):
        df=self.fetch_ohlcv(symbol,tf)
        if df is None or len(df)<60: return
        dft=self.fetch_ohlcv(symbol,cfg["trend_tf"],limit=220)
        if dft is None or len(dft)<200: return
        bias=SMCAnalyzer(dft).trend_bias()
        if bias=="neutral": return
        ctx=self.fetch_ctx(symbol,tf)
        sig=SMCAnalyzer(df).generate_signal(symbol,tf,cfg,ctx,trend_bias=bias)
        if sig and not self.already_alerted(sig,cfg):
            ok=send_telegram(format_signal(sig,cfg), topic_id=cfg["topic"])
            log.info(f"{'OK' if ok else 'ERR'} [{tf}] {symbol} {sig.direction} | "
                     f"{bias} | Lev {sig.lev_low}x-{sig.lev_high}x | TP1 {sig.rr1}x")

    async def run_tf(self, tf):
        cfg=TF_CONFIG[tf]
        log.info(f"[{tf}] Worker listo | trend:{cfg['trend_tf']} | "
                 f"TPs:{cfg['tp_rr']} | maxLev:{cfg['max_leverage']}x")
        send_telegram(
            f"🤖 <b>SMC Bot {cfg['label']} activo</b> ✅\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏱  Señales: <b>{tf}</b> | Tendencia: <b>{cfg['trend_tf']}</b>\n"
            f"📈 EMA{EMA_FAST}/EMA{EMA_SLOW} | R/R mín: <b>{cfg['rr_min']}x</b>\n"
            f"🎯 TPs: <b>{cfg['tp_rr'][0]}x</b>({cfg['tp_pct'][0]}%) | "
            f"<b>{cfg['tp_rr'][1]}x</b>({cfg['tp_pct'][1]}%) | "
            f"<b>{cfg['tp_rr'][2]}x</b>({cfg['tp_pct'][2]}%)\n"
            f"⚡ Lev máx: <b>{cfg['max_leverage']}x</b> | "
            f"Riesgo: <b>{cfg['risk_pct']}%</b> capital\n"
            f"⏲️  Cooldown: <b>{cfg['alert_cooldown_h']}h</b> | "
            f"Scan: cada <b>{cfg['check_interval']}s</b>\n"
            f"🌐 UTC{TZ_OFFSET:+d} | Top {TOP_N_SYMBOLS} pares USDT PERP\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Solo señales a favor de tendencia · OB · FVG · CHoCH · Sweeps</i>",
            topic_id=cfg["topic"],
        )
        while True:
            while not self.symbols: await asyncio.sleep(2)
            t0=time.time()
            log.info(f"── [{tf}] {len(self.symbols)} pares [{datetime.now(TZ).strftime('%H:%M:%S')}] ──")
            for s in self.symbols:
                await self.scan_symbol(s,tf,cfg)
                await asyncio.sleep(0.2)
            wait=max(0, cfg["check_interval"]-(time.time()-t0))
            log.info(f"── [{tf}] próximo en {wait:.0f}s ──")
            await asyncio.sleep(wait)

    async def refresh_loop(self):
        while True:
            await asyncio.sleep(SYMBOLS_REFRESH_H*3600)
            self.refresh_if_needed(force=True)

    async def run(self):
        log.info("="*60)
        log.info("  SMC Bot — 3 TFs en paralelo: 15m | 1h | 4h")
        log.info(f"  15m→Topic {TOPIC_15M} | 1H→Topic {TOPIC_1H} | 4H→Topic {TOPIC_4H}")
        log.info(f"  Top {TOP_N_SYMBOLS} pares USDT PERP | UTC{TZ_OFFSET:+d}")
        log.info("="*60)
        self.refresh_if_needed(force=True)
        await asyncio.gather(
            self.run_tf("15m"),
            self.run_tf("1h"),
            self.run_tf("4h"),
            self.refresh_loop(),
        )


if __name__=="__main__":
    bot=BybitSMCBot()
    try: asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("Detenido.")
        for tf,cfg in TF_CONFIG.items():
            send_telegram(f"🔴 <b>SMC Bot {cfg['label']} detenido</b>", topic_id=cfg["topic"])
