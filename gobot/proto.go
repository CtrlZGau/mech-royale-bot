package main

// mechfight/1 message types and the stdin/stdout loop.

import (
	"bufio"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"runtime/debug"
	"time"
)

type Vec2 [2]float64
type Vec3 [3]float64

type Actor struct {
	ID       int      `json:"id"`
	Kind     string   `json:"kind"`
	Pos      Vec3     `json:"pos"`
	Vel      Vec3     `json:"vel"`
	Aim      *Vec2    `json:"aim"`
	HP       *float64 `json:"hp"`
	MaxHP    *float64 `json:"max_hp"`
	Cubes    float64  `json:"cubes"`
	Weapons  []string `json:"weapons"`
	Charging bool     `json:"charging"`
}

func (a *Actor) hp() float64 {
	if a.HP == nil {
		return 1400
	}
	return *a.HP
}

func (a *Actor) maxHP() float64 {
	if a.MaxHP == nil {
		return 1400
	}
	return *a.MaxHP
}

func (a *Actor) carries(kind string) bool {
	for _, w := range a.Weapons {
		if w == kind {
			return true
		}
	}
	return false
}

type Weapon struct {
	Kind     string             `json:"kind"`
	Cooldown float64            `json:"cooldown"`
	Ammo     *float64           `json:"ammo"`
	Reload   *float64           `json:"reload"`
	Charge   float64            `json:"charge"`
	Stats    map[string]float64 `json:"stats"`
}

func (w *Weapon) ammo(d float64) float64 {
	if w.Ammo == nil {
		return d
	}
	return *w.Ammo
}

func (w *Weapon) reload() float64 {
	if w.Reload == nil {
		return 0
	}
	return *w.Reload
}

func (w *Weapon) st(k string, d float64) float64 { return fget(w.Stats, k, d) }

type Self struct {
	ID        int                `json:"id"`
	Pos       Vec3               `json:"pos"`
	Vel       Vec3               `json:"vel"`
	Aim       Vec2               `json:"aim"`
	HP        float64            `json:"hp"`
	MaxHP     float64            `json:"max_hp"`
	Energy    float64            `json:"energy"`
	MaxEnergy float64            `json:"max_energy"`
	Grounded  bool               `json:"grounded"`
	Cubes     float64            `json:"cubes"`
	Weapons   []*Weapon          `json:"weapons"`
	InZone    bool               `json:"in_zone"`
	Stats     map[string]float64 `json:"stats"`
}

type Item struct {
	ID   int      `json:"id"`
	Kind string   `json:"kind"`
	Pos  Vec3     `json:"pos"`
	HP   *float64 `json:"hp"`
}

type Projectile struct {
	ID   int    `json:"id"`
	Kind string `json:"kind"`
	Pos  Vec3   `json:"pos"`
	Vel  Vec3   `json:"vel"`
}

type Event struct {
	Type    string   `json:"type"`
	Weapon  string   `json:"weapon"`
	Bearing *float64 `json:"bearing"`
	By      *float64 `json:"by"`
	Target  *float64 `json:"target"`
	ID      any      `json:"id"`
}

type Zone struct {
	Center      Vec2    `json:"center"`
	Radius      float64 `json:"radius"`
	NextCenter  Vec2    `json:"next_center"`
	NextRadius  float64 `json:"next_radius"`
	ShrinkStart float64 `json:"shrink_start"`
	ShrinkEnd   float64 `json:"shrink_end"`
}

type Offer struct {
	Choices []struct {
		ID string `json:"id"`
	} `json:"choices"`
}

type Drop struct {
	ID       int      `json:"id"`
	Pos      Vec3     `json:"pos"`
	Radius   float64  `json:"radius"`
	LandsIn  float64  `json:"lands_in"`
	CaptureS *float64 `json:"capture_s"`
	Holder   *float64 `json:"holder"`
}

type Obs struct {
	Type        string          `json:"type"`
	Tick        json.RawMessage `json:"tick"`
	T           float64         `json:"t"`
	Alive       int             `json:"alive"`
	Self        Self            `json:"self"`
	Mechs       []Actor         `json:"mechs"`
	Projectiles []Projectile    `json:"projectiles"`
	Crates      []Item          `json:"crates"`
	Cubes       []Item          `json:"cubes"`
	Pickups     []Item          `json:"pickups"`
	Events      []Event         `json:"events"`
	Zone        Zone            `json:"zone"`
	Offer       *Offer          `json:"offer"`
	Drops       []Drop          `json:"drops"`
}

type Config struct {
	Mech    map[string]float64            `json:"mech"`
	Npc     map[string]float64            `json:"npc"`
	Crates  map[string]float64            `json:"crates"`
	Cubes   map[string]any                `json:"cubes"`
	Loot    map[string]any                `json:"loot"`
	Weapons map[string]map[string]float64 `json:"weapons"`
}

type Hello struct {
	Type string  `json:"type"`
	Seed float64 `json:"seed"`
	Map  struct {
		Dir string `json:"dir"`
	} `json:"map"`
	Config Config `json:"config"`
}

type Action struct {
	Tick   json.RawMessage `json:"tick"`
	Move   *[2]float64     `json:"move,omitempty"`
	Aim    *[2]float64     `json:"aim,omitempty"`
	Fire   *[2]bool        `json:"fire,omitempty"`
	Boost  *bool           `json:"boost,omitempty"`
	Jump   *bool           `json:"jump,omitempty"`
	Reload *bool           `json:"reload,omitempty"`
	Pick   *int            `json:"pick,omitempty"`
	Take   *int            `json:"take,omitempty"`
	Drop   *int            `json:"drop,omitempty"`
	Debug  map[string]any  `json:"debug,omitempty"`
}

func fget(m map[string]float64, k string, d float64) float64 {
	if v, ok := m[k]; ok {
		return v
	}
	return d
}

func aget(m map[string]any, k string, d float64) float64 {
	if v, ok := m[k].(float64); ok {
		return v
	}
	return d
}

func logf(format string, a ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", a...)
}

func round(v float64, p float64) float64 { return math.Round(v*p) / p }

func main() {
	if len(os.Args) > 2 && os.Args[1] == "probe" {
		runProbe(os.Args[2])
		return
	}
	lines := make(chan []byte, 256)
	go func() {
		r := bufio.NewReaderSize(os.Stdin, 1<<20)
		for {
			b, err := r.ReadBytes('\n')
			if len(b) > 0 {
				lines <- b
			}
			if err != nil {
				close(lines)
				return
			}
		}
	}()
	out := bufio.NewWriter(os.Stdout)
	send := func(v any) {
		b, err := json.Marshal(v)
		if err != nil {
			logf("marshal: %v", err)
			return
		}
		out.Write(b)
		out.WriteByte('\n')
		out.Flush()
	}
	var bot *Bot
	var last Action
	errors := 0
	for {
		first, ok := <-lines
		if !ok {
			return
		}
		batch := [][]byte{first}
	drain:
		for {
			select {
			case b, ok := <-lines:
				if !ok {
					break drain
				}
				batch = append(batch, b)
			default:
				break drain
			}
		}
		var latest []byte
		for _, raw := range batch {
			var head struct {
				Type string `json:"type"`
			}
			if json.Unmarshal(raw, &head) != nil {
				continue
			}
			switch head.Type {
			case "hello":
				var h Hello
				if err := json.Unmarshal(raw, &h); err != nil {
					logf("hello parse: %v", err)
				} else {
					func() {
						defer func() {
							if r := recover(); r != nil {
								logf("init failed: %v\n%s", r, debug.Stack())
							}
						}()
						t0 := time.Now()
						bot = NewBot(&h)
						logf("ready in %.2fs", time.Since(t0).Seconds())
					}()
				}
				send(map[string]string{"type": "ready", "name": "warden"})
			case "obs":
				latest = raw
			case "end":
				return
			}
		}
		if latest == nil {
			continue
		}
		var obs Obs
		if err := json.Unmarshal(latest, &obs); err != nil {
			logf("obs parse: %v", err)
			var tk struct {
				Tick json.RawMessage `json:"tick"`
			}
			if json.Unmarshal(latest, &tk) == nil && tk.Tick != nil {
				a := Action{Move: last.Move, Aim: last.Aim, Boost: last.Boost}
				a.Tick = tk.Tick
				send(a) // coast on the last move instead of missing the tick
			}
			continue
		}
		act := Action{}
		if bot != nil {
			func() {
				defer func() {
					if r := recover(); r != nil {
						errors++
						if errors <= 5 || errors%100 == 0 {
							logf("act() failed at t=%.2f (%d so far): %v\n%s", obs.T, errors, r, debug.Stack())
						}
						act = Action{Move: last.Move, Aim: last.Aim, Boost: last.Boost}
					}
				}()
				act = bot.Act(&obs)
				last = Action{Move: act.Move, Aim: act.Aim, Boost: act.Boost}
			}()
		}
		act.Tick = obs.Tick
		send(act)
	}
}
