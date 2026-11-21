package main

import (
	"os"
	"strconv"
	"strings"
)

// Phase profiles and global switches. Defaults are the practice configuration; any number can be changed
// for experiments with WD_P="g.back_s=1.2,hunt.finish_r=14,scout.loot_r=150" (g. = global, else a phase).

func baseProfile() map[string]float64 {
	return map[string]float64{
		"fight_close": 60, "max_range": 215, "hp_weight": 60.0 / 1400, "wounded_bonus": 0, "keep_bonus": 0,
		"pull_hp": 0.38, "steady_hp": 0.8, "pref_gun": 85, "pref_light": 28, "pref_laser": 85,
		"loot_r": 90, "laser_r": 90, "farm_hp": 0.7, "cube_fight_r": 0, "finish_r": 0, "laser_ups": 0,
		"peek": 1, "lethal": 0, "crate_until": 1e9, "laser_first": 1, "speed_ups": 0,
	}
}

func profiles() map[string]map[string]float64 {
	scout := baseProfile()
	guard := baseProfile()
	guard["crate_until"] = 150
	hunt := baseProfile()
	for k, v := range map[string]float64{
		"max_range": 240, "hp_weight": 0.1, "wounded_bonus": 200, "keep_bonus": 200, "pref_laser": 120,
		"loot_r": 150, "laser_r": 240, "farm_hp": 0.5, "cube_fight_r": 70, "finish_r": 20, "laser_ups": 1,
		"peek": 0, "lethal": 1,
	} {
		hunt[k] = v
	}
	return map[string]map[string]float64{"scout": scout, "guard": guard, "hunt": hunt}
}

func globals() map[string]float64 {
	return map[string]float64{
		"scout_s": 20, "guard": 1, "others_dead_ticks": 5, "back_s": 1.5, "lose_two_hp": 0.55,
		"lose_three_hp": 0.8, "lose_low_hp": 0.35, "closing_v": 3, "closing_r": 180, "weak_frac": 0.4,
		"weak_r": 160, "aimed_r": 190, "struck_s": 6, "lethal": 1, "sidestep": 1, "drops": 0,
		"drone_farm": 1, "crate_farm": 1, "cube_v": 75,
		"esc_guard":    0,   // 1 = give up a pocket escape whose walk to the takeoff makes no progress for 2 s (5 s pause)
		"lethal_k":     0.8, // early laser release when this share of the partial-charge damage kills
		"open_recover": 0,   // 1 = in the first 60 s, back off for 5.5 s after losing an exchange
	}
}

func applyEnv(g map[string]float64, prof map[string]map[string]float64) {
	for _, kv := range strings.Split(os.Getenv("WD_P"), ",") {
		key, val, ok := strings.Cut(kv, "=")
		if !ok {
			continue
		}
		sect, name, _ := strings.Cut(strings.TrimSpace(key), ".")
		v, err := strconv.ParseFloat(strings.TrimSpace(val), 64)
		if err != nil {
			continue
		}
		book := g
		if sect != "g" {
			book = prof[sect]
		}
		if book == nil {
			continue
		}
		if _, ok := book[name]; ok {
			book[name] = v
		}
	}
}

var upgGunLine = []string{"plating", "armor", "gun_damage", "nanites", "gun_rate", "lightning_efficiency",
	"lightning_range", "laser_damage", "servos", "laser_charge", "gun_mag", "gun_velocity", "gyros",
	"lightning_chain", "reactor", "capacitor", "mortar_reload", "mortar_splash", "mortar_velocity", "sensors",
	"jets", "laser_pierce"}
var upgLaserLine = []string{"plating", "armor", "laser_damage", "laser_charge", "nanites", "gun_damage",
	"gun_rate", "lightning_efficiency", "lightning_range", "servos", "gun_mag", "gun_velocity", "gyros",
	"lightning_chain", "reactor", "capacitor", "mortar_reload", "mortar_splash", "mortar_velocity", "sensors",
	"jets", "laser_pierce"}
var upgSpeedLine = []string{"plating", "armor", "servos", "jets", "reactor", "laser_damage", "laser_charge",
	"nanites", "gun_damage", "gun_rate", "lightning_efficiency", "lightning_range", "gun_mag", "gun_velocity",
	"gyros", "lightning_chain", "capacitor", "mortar_reload", "mortar_splash", "mortar_velocity", "sensors",
	"laser_pierce"}

var slotValue = map[string]float64{"laser": 4, "lightning": 3, "mortar": 0}
var slotValuePlain = map[string]float64{"lightning": 3, "laser": 2, "mortar": 0}
