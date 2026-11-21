package main

// Warden: a squad-hunter policy (Scout, then Hunt; Guard once a death we did not cause is seen).
// Each decision: memory -> upgrade -> threat picture & target -> zone timing -> mode -> movement
// (pocket escape, stuck recovery, shell dodge, sidestep) -> fire control -> reload -> weapon pickup.

import (
	"fmt"
	"math"
	"math/rand"
	"sort"
	"strconv"
)

const latency = 0.075 // seconds between our decision and the shot leaving

type Contact struct {
	Actor
	T    float64
	d    float64
	shot bool
	hot  bool
}

type Kept struct {
	Pos  Vec3
	T    float64
	Kind string
}

type attack struct {
	t float64
}

type pick struct {
	kind string
	id   int
	pos  Vec3
}

type Bot struct {
	cfg  Config
	rng  *rand.Rand
	L    *Land
	R    router
	g    map[string]float64
	prof map[string]map[string]float64
	k    map[string]float64

	phase     string
	lastHitT  float64
	attackers map[int]attack
	closing   map[int]float64 // last measured approach speed per mech (kept while closer than 1 m)
	enemySeen *omap[*Contact]
	items     map[string]*omap[*Kept]
	drones    *omap[*Contact]

	keepID              int
	keepT, lostAt       float64
	backing             bool
	dangerT             float64
	droneHitT, droneBrg float64
	chaseID             int // loot target being chased (by id), -1 = none
	chaseT              float64
	holdC               Vec2    // zone circle at the start of the current shrink
	holdR, holdFrom     float64 // radius of that circle; shrink_start it belongs to (-1 = none yet)
	banned              map[int]float64
	side, sideUntil     float64
	post                *Vec2
	postT               float64
	alive0              int
	othersDeadTicks     int
	guard               bool
	coverSpot           *Vec2
	shelterFound        bool
	coverT              float64
	myKills             int
	painDir             *float64
	droneStruck         map[int]float64
	stallStart          float64
	jiggleEnd           float64
	jiggleDir           Vec2
	wpath               []Vec2
	wpathSet            bool
	wpathGoal           *Vec2
	wpathT              float64
	wpathI              int
	touchdown, escFrom  *Vec2
	jetting             bool
	escT, escOffUntil   float64
	escTrackOn          bool
	lateGhost           bool // end_ghost: cautious profile until the end of a practice match
	escTrack            [3]float64
	mode                string
	attnBrg, attnT      float64 // watch: bearing and time of the latest threat sound worth looking at
	attnPri             int
	dangerPts           []Vec3 // snipe_mem: estimated shooter spots (x, y, time)

	t   float64
	me  *Self
	pos Vec3
	obs *Obs
}

func NewBot(h *Hello) *Bot {
	b := &Bot{cfg: h.Config, rng: rand.New(rand.NewSource(seedOf(h))),
		g: globals(), prof: profiles(), phase: "scout",
		lastHitT: -99, attackers: map[int]attack{}, closing: map[int]float64{}, enemySeen: newOmap[*Contact](),
		items:  map[string]*omap[*Kept]{"crate": newOmap[*Kept](), "cube": newOmap[*Kept](), "pickup": newOmap[*Kept]()},
		drones: newOmap[*Contact](), keepID: -1, keepT: -99, lostAt: -99, dangerT: -99, droneHitT: -99,
		chaseID: -1, chaseT: -99, banned: map[int]float64{}, side: 1, postT: -99, alive0: -1, coverT: -99, holdFrom: -1,
		droneStruck: map[int]float64{}, stallStart: -1, jiggleEnd: -1, wpathT: -99, escT: -99,
		escOffUntil: -1, mode: "start", attnT: -99}
	applyEnv(b.g, b.prof)
	b.k = copyMap(b.prof["scout"])
	dir := h.Map.Dir
	if dir == "" {
		dir = "/arena"
	}
	L, err := LoadLand(dir)
	if err != nil {
		logf("map load failed (%v): playing on a flat arena", err)
		L = FlatLand()
	}
	b.L = L
	return b
}

// seedOf: the per-bot seed from hello (0 -> slot + 1), as a signed 64-bit RNG seed.
func seedOf(h *Hello) int64 {
	var u uint64
	if v, err := strconv.ParseUint(string(h.Seed), 10, 64); err == nil {
		u = v
	} else if f, err := strconv.ParseFloat(string(h.Seed), 64); err == nil && f > 0 {
		u = uint64(f)
	}
	if u == 0 {
		return int64(h.Slot) + 1
	}
	return int64(u)
}

func copyMap(m map[string]float64) map[string]float64 {
	c := make(map[string]float64, len(m))
	for k, v := range m {
		c[k] = v
	}
	return c
}

// ================================================================== small helpers
func wrap(a float64) float64 {
	a = math.Mod(a+math.Pi, 2*math.Pi)
	if a < 0 {
		a += 2 * math.Pi
	}
	return a - math.Pi
}

func flat(a, b Vec3) float64 { return math.Hypot(a[0]-b[0], a[1]-b[1]) }

func unit(x, y float64) Vec2 {
	d := math.Hypot(x, y)
	if d > 1e-6 {
		return Vec2{x / d, y / d}
	}
	return Vec2{}
}

func moving(v Vec2) bool { return v[0] != 0 || v[1] != 0 }

func (b *Bot) st(k string, _ float64) float64 {
	if b.me.Stats["radius"] == 0 {
		return b.cfg.Mech[k]
	}
	return b.me.Stats[k]
}

func (b *Bot) scale(cubes float64) float64 {
	return 1 + aget(b.cfg.Cubes, "size_per_cube", 0.05)*cubes
}

func (b *Bot) bodyCenter(m *Actor) Vec3 {
	if m.Kind == "npc" {
		return Vec3{m.Pos[0], m.Pos[1], m.Pos[2] + fget(b.cfg.Npc, "height", 3.5)*0.5}
	}
	return Vec3{m.Pos[0], m.Pos[1], m.Pos[2] + fget(b.cfg.Mech, "height", 9)*b.scale(m.Cubes)*0.5}
}

func (b *Bot) eyeOf(m *Actor) Vec3 {
	return Vec3{m.Pos[0], m.Pos[1], m.Pos[2] + fget(b.cfg.Mech, "eye_height", 8)*b.scale(m.Cubes)}
}

func (b *Bot) muzzle() Vec3 {
	return Vec3{b.pos[0] + b.me.Vel[0]*latency, b.pos[1] + b.me.Vel[1]*latency, b.pos[2] + b.st("muzzle_height", 7)}
}

func (b *Bot) plainMuzzle() Vec3 {
	return Vec3{b.pos[0], b.pos[1], b.pos[2] + b.st("muzzle_height", 7)}
}

func (b *Bot) clearShot(m *Actor) bool {
	mz := b.plainMuzzle()
	if b.L.FireLane(mz, b.bodyCenter(m)) {
		return true
	}
	if m.Kind == "mech" {
		top := Vec3{m.Pos[0], m.Pos[1], m.Pos[2] + fget(b.cfg.Mech, "height", 9)*b.scale(m.Cubes)*0.85}
		return b.L.FireLane(mz, top)
	}
	return false
}

func (b *Bot) gear(kind string) (int, *Weapon) {
	for i, w := range b.me.Weapons {
		if w != nil && w.Kind == kind {
			return i, w
		}
	}
	return -1, nil
}

func (b *Bot) slot(i int) *Weapon {
	if i < len(b.me.Weapons) {
		return b.me.Weapons[i]
	}
	return nil
}

func (b *Bot) aimingAtMe(m *Actor, tol float64) bool {
	if m.Aim == nil {
		return false
	}
	br := math.Atan2(b.pos[1]-m.Pos[1], b.pos[0]-m.Pos[0])
	return math.Abs(wrap(m.Aim[0]-br)) < tol
}

// approachSpeed: how fast the mech closes on us; when it is within 1 m the last measurement stands.
func (b *Bot) approachSpeed(m *Actor) float64 {
	dx, dy := m.Pos[0]-b.pos[0], m.Pos[1]-b.pos[1]
	d := math.Hypot(dx, dy)
	if d > 1 {
		b.closing[m.ID] = -((m.Vel[0]-b.me.Vel[0])*dx + (m.Vel[1]-b.me.Vel[1])*dy) / d
	}
	return b.closing[m.ID]
}

func (b *Bot) struckAgo(id int) float64 {
	if a, ok := b.attackers[id]; ok {
		return b.t - a.t
	}
	return 1e9
}

// ================================================================== memory
// noteShooter (snipe_mem): a hit from a mech we cannot see right now, or taken while not fighting, marks the
// shooter's rough spot (from the hit bearing); waiting spots in its line of fire are avoided for 40 s.
func (b *Bot) noteShooter(id int, brg float64) {
	t := b.t
	seenNow := false
	dist := 100.0
	if e, ok := b.enemySeen.get(id); ok && t-e.T < 0.3 {
		seenNow = true
		dist = math.Max(30, flat(e.Pos, b.pos))
	}
	if seenNow && b.mode == "fight" {
		return
	}
	p := Vec3{b.pos[0] + math.Cos(brg)*dist, b.pos[1] + math.Sin(brg)*dist, t}
	for i, q := range b.dangerPts {
		if math.Hypot(q[0]-p[0], q[1]-p[1]) < 25 {
			b.dangerPts[i] = p
			p[2] = -1
			break
		}
	}
	if p[2] >= 0 {
		b.dangerPts = append(b.dangerPts, p)
		if len(b.dangerPts) > 6 {
			b.dangerPts = b.dangerPts[1:]
		}
	}
	// the current waiting spot is in that line of fire: choose a new one
	if b.post != nil && b.exposedTo(b.post[0], b.post[1], b.dangerPts[len(b.dangerPts)-1]) {
		b.post, b.postT = nil, -99
	}
	if b.post != nil && flat(Vec3{b.post[0], b.post[1], 0}, b.pos) < 25 {
		b.post, b.postT = nil, -99 // hit at the post itself
	}
}

// exposedTo: a mech standing at (x, y) can be shot from the danger point (within 160 m, clear lane).
func (b *Bot) exposedTo(x, y float64, dp Vec3) bool {
	if math.Hypot(x-dp[0], y-dp[1]) > 160 {
		return false
	}
	L := b.L
	from := Vec3{dp[0], dp[1], L.TreadZ(dp[0], dp[1]) + b.st("muzzle_height", 7)}
	to := Vec3{x, y, L.TreadZ(x, y) + b.st("height", 9)*0.6}
	return L.FireLane(from, to)
}

// watchFoe (watch): the nearest mech seen in the last 15 s within 220 m (position as last seen).
func (b *Bot) watchFoe() *Contact {
	var best *Contact
	bd := 220.0
	b.enemySeen.each(func(_ int, e *Contact) {
		if b.t-e.T > 15 {
			return
		}
		if d := flat(e.Pos, b.pos); d < bd {
			best, bd = e, d
		}
	})
	return best
}

// hearSounds (watch): keep the most urgent recent threat sound (laser charging > shots > footsteps) that does not
// come from a mech we can already see.
func (b *Bot) hearSounds(obs *Obs) {
	t := b.t
	for _, s := range obs.Sounds {
		pri := 0
		switch s.Kind {
		case "laser_charge":
			pri = 3
		case "laser", "gun", "lightning":
			if s.Dist != "far" {
				pri = 2
			}
		case "step", "boost", "jet":
			pri = 1
		}
		if pri == 0 || s.Age > 1.5 {
			continue
		}
		seen := false
		for i := range obs.Mechs {
			m := &obs.Mechs[i]
			if m.Kind != "mech" {
				continue
			}
			if math.Abs(wrap(math.Atan2(m.Pos[1]-b.pos[1], m.Pos[0]-b.pos[0])-s.Bearing)) < 0.3 {
				seen = true
				break
			}
		}
		if seen {
			continue
		}
		st := t - s.Age
		if pri > b.attnPri || st-b.attnT > 1.5 || (pri == b.attnPri && st >= b.attnT) {
			b.attnBrg, b.attnT, b.attnPri = s.Bearing, st, pri
		}
	}
	if t-b.attnT > 3 {
		b.attnPri = 0
	}
}

func (b *Bot) remember(obs *Obs) {
	t := b.t
	if b.g["watch"] != 0 {
		b.hearSounds(obs)
	}
	if len(b.dangerPts) > 0 && t-b.dangerPts[0][2] > 40 {
		b.dangerPts = b.dangerPts[1:]
	}
	// mechs in view; a mech unseen for 20 s is forgotten, including that it hit us
	for i := range obs.Mechs {
		m := obs.Mechs[i]
		if m.Kind == "mech" {
			b.enemySeen.set(m.ID, &Contact{Actor: m, T: t})
		}
	}
	b.enemySeen.each(func(id int, e *Contact) {
		if t-e.T > 20 {
			b.enemySeen.del(id)
			delete(b.attackers, id)
			delete(b.closing, id)
		}
	})
	eye := Vec3{b.pos[0], b.pos[1], b.pos[2] + b.st("eye_height", 8)}
	for _, kl := range []struct {
		key string
		lst []Item
	}{{"crate", obs.Crates}, {"cube", obs.Cubes}, {"pickup", obs.Pickups}} {
		book := b.items[kl.key]
		for _, it := range kl.lst {
			book.set(it.ID, &Kept{Pos: it.Pos, T: t, Kind: it.Kind})
		}
		book.each(func(id int, e *Kept) {
			if e.T == t {
				return
			}
			if flat(e.Pos, b.pos) < 40 && t-e.T > 0.35 && b.L.FireLane(eye, Vec3{e.Pos[0], e.Pos[1], e.Pos[2] + 1.5}) {
				book.del(id) // should be visible but isn't: gone
			}
		})
	}
	for i := range obs.Mechs {
		m := obs.Mechs[i]
		if m.Kind == "npc" {
			// line of fire and distance are measured now, when the drone is seen
			d := &Contact{Actor: m, T: t, d: flat(m.Pos, b.pos)}
			d.shot = b.clearShot(&d.Actor)
			b.drones.set(m.ID, d)
		}
	}
	b.drones.each(func(id int, d *Contact) {
		if t-d.T > 120 {
			b.drones.del(id)
			delete(b.droneStruck, id)
		}
	})
	for _, ev := range obs.Events {
		switch ev.Type {
		case "damaged":
			if ev.Weapon == "npc" && ev.Bearing != nil {
				b.droneHitT, b.droneBrg = t, *ev.Bearing
			}
			if ev.By != nil { // only hits with a known shooter count as being hurt
				b.lastHitT = t
				if ev.Bearing != nil {
					v := *ev.Bearing
					b.painDir = &v
				}
				// "hit us" is remembered only for a mech or drone we already know
				id := int(*ev.By)
				if b.g["snipe_mem"] != 0 && ev.Bearing != nil && ev.Weapon != "npc" {
					b.noteShooter(id, *ev.Bearing)
				}
				if _, ok := b.enemySeen.get(id); ok {
					b.attackers[id] = attack{t}
				}
				if _, ok := b.drones.get(id); ok {
					b.droneStruck[id] = t
				}
			}
		case "kill":
			b.myKills++
			if ev.Target != nil {
				b.enemySeen.del(int(*ev.Target))
				delete(b.attackers, int(*ev.Target))
				delete(b.closing, int(*ev.Target))
			}
		}
	}
	if b.alive0 < 0 {
		b.alive0 = obs.Alive
	}
	if b.alive0-obs.Alive-b.myKills > 0 {
		b.othersDeadTicks++
	} else {
		b.othersDeadTicks = 0
	}
	if b.g["guard"] != 0 && float64(b.othersDeadTicks) >= b.g["others_dead_ticks"] {
		b.guard = true
	}
	// remember the circle a shrink starts from (the observation's own circle moves while it shrinks)
	if z := &obs.Zone; b.holdFrom != z.ShrinkStart || t <= z.ShrinkStart {
		b.holdFrom, b.holdC, b.holdR = z.ShrinkStart, z.Center, z.Radius
	}
	want := "scout"
	if b.guard {
		want = "guard"
	} else if t > b.g["scout_s"] {
		want = "hunt"
	}
	// end_ghost (switch): late in a practice match stop hunting the squad and outlast it in the zone collapse
	if eg := b.g["end_ghost"]; want == "hunt" && !b.lateGhost && eg > 0 && obs.Zone.NextRadius <= eg {
		b.lateGhost = true
	}
	if b.lateGhost {
		want = "guard"
	}
	if want != b.phase {
		b.phase = want
		b.k = copyMap(b.prof[want])
	}
}

// ================================================================== zone timing
// zoneHold: the safe circle at time t of the current phase, moving from the held start circle to the next one.
func (b *Bot) zoneHold(z *Zone, t float64) (Vec2, float64) {
	if t <= z.ShrinkStart || z.ShrinkEnd <= z.ShrinkStart {
		return b.holdC, b.holdR
	}
	f := math.Max(0, math.Min(1, (t-z.ShrinkStart)/(z.ShrinkEnd-z.ShrinkStart)))
	return Vec2{b.holdC[0] + (z.NextCenter[0]-b.holdC[0])*f, b.holdC[1] + (z.NextCenter[1]-b.holdC[1])*f},
		b.holdR + (z.NextRadius-b.holdR)*f
}

// zoneNow: the safe circle right now (the observed one if it already differs from the held start circle).
func (b *Bot) zoneNow(z *Zone) (Vec2, float64) {
	if b.t <= z.ShrinkStart || math.Abs(z.Radius-b.holdR) > 0.01 || math.Abs(z.Center[0]-b.holdC[0]) > 0.01 {
		return z.Center, z.Radius
	}
	return b.zoneHold(z, b.t)
}

func (b *Bot) mustLeaveBy(z *Zone, x, y, pad float64) float64 {
	nc, nr := z.NextCenter, z.NextRadius
	if math.Hypot(x-nc[0], y-nc[1]) <= nr-pad {
		return math.Inf(1)
	}
	for k := 0; k < 25; k++ {
		tt := z.ShrinkStart + (z.ShrinkEnd-z.ShrinkStart)*float64(k)/24
		c, r := b.zoneHold(z, tt)
		if math.Hypot(x-c[0], y-c[1]) > r-pad {
			return tt
		}
	}
	return z.ShrinkEnd
}

func safePoint(z *Zone, x, y float64) (float64, float64) {
	nc, nr := z.NextCenter, z.NextRadius
	if nr <= 30 {
		return nc[0], nc[1]
	}
	margin := math.Max(3, math.Min(30, 0.3*nr))
	dx, dy := x-nc[0], y-nc[1]
	d := math.Hypot(dx, dy)
	r := math.Max(0, nr-margin-5)
	if d < 1 || d <= r {
		return x, y
	}
	return nc[0] + dx/d*r, nc[1] + dy/d*r
}

func (b *Bot) zoneCost(z *Zone, x, y float64) float64 {
	c, r := b.zoneNow(z)
	cost := 0.0
	if out := math.Hypot(x-c[0], y-c[1]) - r; out > -5 {
		cost += 150 + 5*(out+5)
	}
	if outn := math.Hypot(x-z.NextCenter[0], y-z.NextCenter[1]) - z.NextRadius; outn > -5 {
		urgency := math.Max(0.1, math.Min(1, 1-(z.ShrinkStart-b.t)/25))
		cost += urgency * (25 + 0.8*(outn+5))
	}
	return cost
}

// ================================================================== threat picture
func (b *Bot) appraise(obs *Obs, endgame bool) (best *Contact, vis []*Contact, danger bool, crowd int, unseen *Contact) {
	t := b.t
	K := b.k
	G := b.g
	pickScore := 1e18
	pokeOK := false
	if G["poke_r"] > 0 && b.phase != "hunt" && b.me.HP > G["poke_hp"]*b.me.MaxHP {
		if _, las := b.gear("laser"); las != nil {
			near := 0
			b.enemySeen.each(func(_ int, e *Contact) {
				if e.T == t && flat(e.Pos, b.pos) < 170 {
					near++
				}
			})
			pokeOK = near <= 1
		}
	}
	b.enemySeen.each(func(_ int, e *Contact) {
		if t-e.T > 2.5 {
			return
		}
		vis = append(vis, e)
		if e.T != t {
			e.shot, e.hot = false, false
			return
		}
		d := flat(e.Pos, b.pos)
		e.d = d
		e.shot = b.clearShot(&e.Actor)
		hp, mhp := e.hp(), math.Max(e.maxHP(), 1)
		pointedAtUs := b.aimingAtMe(&e.Actor, 0.22)
		closing := b.approachSpeed(&e.Actor)
		struck := b.struckAgo(e.ID)
		var hot bool
		if b.phase == "hunt" {
			hot = d < K["fight_close"] || struck < G["struck_s"] || endgame ||
				(closing > G["closing_v"] && d < G["closing_r"]) ||
				(hp < G["weak_frac"]*mhp && d < G["weak_r"]) || (pointedAtUs && d < G["aimed_r"])
		} else {
			hot = d < K["fight_close"] || struck < 4 || (pointedAtUs && e.Charging && d < 120) ||
				(hp < 350 && d < 120) || obs.Alive <= 2
			if !hot && pokeOK && d < G["poke_r"] {
				hot = true // laser poke: open on a lone foe at range
			}
		}
		e.hot = hot
		if hot && !e.shot && d < K["max_range"] && (unseen == nil || d < unseen.d) {
			unseen = e
		}
		if !hot || !e.shot || d > K["max_range"] {
			return
		}
		s := d + K["hp_weight"]*hp
		if K["wounded_bonus"] != 0 && hp <= 0.3*mhp {
			s -= K["wounded_bonus"]
		}
		if K["keep_bonus"] != 0 && e.ID == b.keepID && t-b.keepT < 2 {
			s -= K["keep_bonus"]
		}
		if struck < 4 {
			s -= 40
		}
		if closing > 3 {
			s -= 15
		}
		if s < pickScore {
			best, pickScore = e, s
		}
	})
	if best != nil {
		b.keepID, b.keepT = best.ID, t
	}
	for _, e := range vis {
		if e.hot || b.struckAgo(e.ID) < 4 {
			danger = true
		}
		if e.T == t && (e.hot || (e.shot && e.d < 150)) {
			crowd++
		}
	}
	if danger || t-b.lastHitT < 3 {
		b.dangerT = t
	}
	return
}

// ================================================================== movement pieces
func (b *Bot) goTo(gx, gy float64) Vec2 {
	L := b.L
	x, y := b.pos[0], b.pos[1]
	d := math.Hypot(gx-x, gy-y)
	if d < 0.8 {
		return Vec2{}
	}
	if b.pos[2]-L.FloorZ(x, y) > 3 && L.hindered[L.FineIdx(x, y)] {
		return unit(gx-x, gy-y)
	}
	if d < 45 && L.StrollOK(x, y, gx, gy) {
		return unit(gx-x, gy-y)
	}
	if !b.wpathSet || b.wpathGoal == nil || math.Hypot(b.wpathGoal[0]-gx, b.wpathGoal[1]-gy) > 5 || b.t-b.wpathT > 2.5 {
		routeCells := L.Route(&b.R, x, y, gx, gy, 22000)
		b.wpath = b.wpath[:0]
		for _, c := range routeCells {
			cx, cy := L.CellXY(c)
			b.wpath = append(b.wpath, Vec2{cx, cy})
		}
		b.wpathSet, b.wpathI = routeCells != nil, 0
		b.wpathGoal, b.wpathT = &Vec2{gx, gy}, b.t
	}
	path := b.wpath
	if len(path) == 0 {
		return unit(gx-x, gy-y)
	}
	bi, bd := b.wpathI, 1e18
	for i := b.wpathI; i < min(len(path), b.wpathI+10); i++ {
		if dd := math.Hypot(path[i][0]-x, path[i][1]-y); dd < bd {
			bi, bd = i, dd
		}
	}
	b.wpathI = bi
	if bd > 12 {
		b.wpathSet = false
		return unit(gx-x, gy-y)
	}
	for i := min(bi+14, len(path)-1); i > bi; i-- {
		px, py := path[i][0], path[i][1]
		if L.StrollOK(x, y, px, py) {
			if i == len(path)-1 && math.Hypot(px-gx, py-gy) < 4 && L.StrollOK(x, y, gx, gy) {
				return unit(gx-x, gy-y)
			}
			return unit(px-x, py-y)
		}
	}
	if bi+1 < len(path) {
		return unit(path[bi+1][0]-x, path[bi+1][1]-y)
	}
	return unit(gx-x, gy-y)
}

func (b *Bot) keepRange(m *Actor, override float64) float64 {
	_, light := b.gear("lightning")
	_, laser := b.gear("laser")
	K := b.k
	if m.Kind == "npc" {
		return 58
	}
	pg, pl, pz := K["pref_gun"], K["pref_light"], K["pref_laser"]
	if override > 0 {
		pg, pl, pz = override, override, override
	}
	r := pg
	if light != nil && b.me.Energy > 12 {
		r = pl
	} else if laser != nil {
		r = pz
	}
	if light == nil {
		if m.carries("lightning") {
			r = math.Max(r, 64)
		}
		if m.carries("mortar") {
			r = math.Max(r, 80)
		}
	}
	return r
}

// brawlStep: the best nearby spot to fight from (hold range, keep or break line of fire, strafe, stay out
// of other enemies' sight, stay in the zone, drift toward cubes).
func (b *Bot) brawlStep(obs *Obs, foe *Actor, vis []*Contact, wantSight, override float64) Vec2 {
	L := b.L
	x, y := b.pos[0], b.pos[1]
	wantRange := b.keepRange(foe, override)
	bx, by := foe.Pos[0]-x, foe.Pos[1]-y
	bd := math.Hypot(bx, by) + 1e-9
	ux, uy := bx/bd, by/bd
	if b.t >= b.sideUntil {
		if b.rng.Float64() < 0.6 {
			b.side = -b.side
		}
		b.sideUntil = b.t + 0.3 + b.rng.Float64()*0.7
	}
	sx, sy := -uy*b.side, ux*b.side
	center := b.bodyCenter(foe)
	mh := b.st("muzzle_height", 7)
	var cube *Kept
	b.items["cube"].each(func(_ int, c *Kept) {
		dc := flat(c.Pos, b.pos)
		if dc < 40 && math.Abs(c.Pos[2]-b.pos[2]) < 4 && (cube == nil || dc < flat(cube.Pos, b.pos)) {
			cube = c
		}
	})
	ringCount := 1
	if b.k["peek"] != 0 {
		ringCount = 2
	}
	best, mv := -1e18, Vec2{}
	sideBlocked := false
	bodyH := b.st("height", 9) * 0.6
	for k := -1; k < 16*ringCount; k++ {
		var cx, cy, dx, dy, r float64
		if k < 0 {
			cx, cy = x, y
		} else {
			a := float64(k%16) * math.Pi / 8
			r = 6.0 * float64(1+k/16)
			dx, dy = math.Cos(a), math.Sin(a)
			cx, cy = x+dx*r, y+dy*r
			if !L.StrollOK(x, y, cx, cy) || !L.OpenAt(cx, cy) {
				if k < 16 && dx*sx+dy*sy > 0.9 {
					sideBlocked = true
				}
				continue
			}
		}
		cz := L.TreadZ(cx, cy)
		dd := math.Hypot(foe.Pos[0]-cx, foe.Pos[1]-cy)
		sc := -1.1*math.Abs(dd-wantRange) - r*0.4
		sight := L.FireLane(Vec3{cx, cy, cz + mh}, center)
		if sight {
			sc += 45 * wantSight
		}
		if ringCount > 1 && k >= 0 && k < 16 && sight == (wantSight > 0) {
			for q := 0; q < 16; q += 2 {
				qx, qy := cx+math.Cos(float64(q)*math.Pi/8)*7, cy+math.Sin(float64(q)*math.Pi/8)*7
				if L.OpenAt(qx, qy) && L.FireLane(Vec3{qx, qy, L.TreadZ(qx, qy) + mh}, center) != sight {
					sc += 8
					break
				}
			}
		}
		torso := Vec3{cx, cy, cz + bodyH}
		for _, e := range vis {
			if &e.Actor == foe || math.Hypot(e.Pos[0]-cx, e.Pos[1]-cy) > 170 {
				continue
			}
			if L.FireLane(b.eyeOf(&e.Actor), torso) {
				sc -= 30
			}
		}
		sc -= b.zoneCost(&obs.Zone, cx, cy)
		if cube != nil {
			sc += 1.5 * (flat(cube.Pos, b.pos) - math.Hypot(cube.Pos[0]-cx, cube.Pos[1]-cy))
		}
		if cl := float64(L.margin[L.CellAt(cx, cy)]); cl < 4 {
			sc -= (4 - cl) * 2.5
		}
		if wantSight > 0 {
			sc += 9 * (dx*sx + dy*sy)
		}
		if k < 0 {
			sc -= 8
			if wantSight < 0 && !sight {
				sc += 14
			}
		}
		if sc > best {
			best, mv = sc, Vec2{dx, dy}
		}
	}
	if sideBlocked {
		b.side = -b.side
		b.sideUntil = b.t + 0.5
	}
	return mv
}

// takeCover: walk to the nearest reachable cell hidden from the (up to 4) nearest threats; stay if already
// hidden and zone-safe. Returns ok=false when there is nothing to hide from or no hidden cell is found.
func (b *Bot) takeCover(vis []*Contact) (Vec2, bool) {
	L := b.L
	var threats []*Contact
	for _, e := range vis {
		if flat(e.Pos, b.pos) < 230 {
			threats = append(threats, e)
		}
	}
	if len(threats) == 0 {
		return Vec2{}, false
	}
	if len(threats) > 4 {
		sort.SliceStable(threats, func(i, j int) bool { return flat(threats[i].Pos, b.pos) < flat(threats[j].Pos, b.pos) })
		threats = threats[:4]
	}
	if b.shelterFound && b.t-b.coverT < 0.4 && b.coverSpot != nil {
		return b.walkToCover(), true
	}
	b.coverT = b.t
	eyes := make([]Vec3, len(threats))
	for i, e := range threats {
		eyes[i] = b.eyeOf(&e.Actor)
	}
	mechH := b.st("height", 9)
	z := &b.obs.Zone
	concealed := func(cx, cy float64) bool {
		cz := L.TreadZ(cx, cy)
		for _, ey := range eyes {
			if L.FireLane(ey, Vec3{cx, cy, cz + mechH}) || L.FireLane(ey, Vec3{cx, cy, cz + mechH*0.5}) {
				return false
			}
		}
		return true
	}
	x, y := b.pos[0], b.pos[1]
	if concealed(x, y) && b.zoneCost(z, x, y) == 0 {
		b.shelterFound, b.coverSpot = true, &Vec2{x, y}
		return Vec2{}, true
	}
	s0 := L.OpenNear(L.CellAt(x, y))
	if s0 < 0 {
		return Vec2{}, false
	}
	cols, rows := L.cols, L.rows
	depth := map[int]int{s0: 0}
	frontier := []int{s0}
	pickCell, pickScore := -1, 1e18
	looked := 0
	for head := 0; head < len(frontier) && looked < 350; head++ {
		c := frontier[head]
		steps := depth[c]
		if steps > 50 || (pickCell >= 0 && float64(steps*2) > pickScore+30) {
			break
		}
		ci, cj := c%cols, c/cols
		if (ci+cj)%2 == 0 && steps > 0 {
			cx, cy := L.CellXY(c)
			looked++
			if concealed(cx, cy) {
				sc := float64(steps*2) + b.zoneCost(z, cx, cy)
				near := math.Inf(1)
				for _, e := range threats {
					near = math.Min(near, math.Hypot(e.Pos[0]-cx, e.Pos[1]-cy))
				}
				sc -= math.Min(near, 80) * 0.25
				if sc < pickScore {
					pickCell, pickScore = c, sc
				}
			}
		}
		for hi, h := range hops {
			i2, j2 := ci+h[0], cj+h[1]
			if i2 < 0 || j2 < 0 || i2 >= cols || j2 >= rows {
				continue
			}
			nb := j2*cols + i2
			if _, ok := depth[nb]; ok || !L.CanHop(c, nb, hi >= 4) {
				continue
			}
			if hi >= 4 && (L.sealed[cj*cols+i2] || L.sealed[j2*cols+ci]) {
				continue
			}
			depth[nb] = steps + 1
			frontier = append(frontier, nb)
		}
	}
	if pickCell < 0 {
		b.shelterFound = false
		return Vec2{}, false
	}
	cx, cy := L.CellXY(pickCell)
	b.shelterFound, b.coverSpot = true, &Vec2{cx, cy}
	return b.walkToCover(), true
}

func (b *Bot) walkToCover() Vec2 {
	if math.Hypot(b.coverSpot[0]-b.pos[0], b.coverSpot[1]-b.pos[1]) < 1.5 {
		return Vec2{}
	}
	return b.goTo(b.coverSpot[0], b.coverSpot[1])
}

// sidestep: lateral dodge from incoming bullets and from lasers charging at us.
func (b *Bot) sidestep(obs *Obs) (float64, float64, bool) {
	cx, cy, cz := b.pos[0], b.pos[1], b.pos[2]+b.st("height", 9)*0.56
	reach := b.st("radius", 2.5) + 1.1
	dx, dy := 0.0, 0.0
	las := false
	for _, p := range obs.Projectiles {
		if p.Kind == "mortar" {
			continue
		}
		vx, vy, vz := p.Vel[0], p.Vel[1], p.Vel[2]
		v2 := vx*vx + vy*vy + vz*vz
		if v2 < 1 {
			continue
		}
		tc := ((cx-p.Pos[0])*vx + (cy-p.Pos[1])*vy + (cz-p.Pos[2])*vz) / v2
		if tc <= 0 || tc > 0.7 {
			continue
		}
		ox, oy := p.Pos[0]+vx*tc-cx, p.Pos[1]+vy*tc-cy
		if math.Hypot(ox, oy) < reach {
			vh := math.Hypot(vx, vy)
			if vh < 1 {
				continue
			}
			px, py := -vy/vh, vx/vh
			if px*ox+py*oy > 0 {
				px, py = -px, -py
			}
			dx += px
			dy += py
		}
	}
	for i := range obs.Mechs {
		m := &obs.Mechs[i]
		if m.Kind != "mech" || !m.Charging || m.Aim == nil {
			continue
		}
		bearingToUs := math.Atan2(cy-m.Pos[1], cx-m.Pos[0])
		if math.Abs(wrap(m.Aim[0]-bearingToUs)) > 0.15 {
			continue
		}
		dx += math.Cos(bearingToUs+math.Pi/2*b.side) * 1.5
		dy += math.Sin(bearingToUs+math.Pi/2*b.side) * 1.5
		las = true
	}
	L := math.Hypot(dx, dy)
	if L > 0.01 {
		return dx / L, dy / L, las
	}
	return 0, 0, false
}

func (b *Bot) postPrice(obs *Obs, px, py float64) float64 {
	z := &obs.Zone
	nc, nr := z.NextCenter, z.NextRadius
	crowd := 0.0
	b.enemySeen.each(func(_ int, e *Contact) {
		if b.t-e.T < 20 {
			crowd += math.Max(0, 80-math.Hypot(e.Pos[0]-px, e.Pos[1]-py)) / 80
		}
	})
	for _, d := range obs.Drops {
		crowd += math.Max(0, 60-math.Hypot(d.Pos[0]-px, d.Pos[1]-py)) / 60
	}
	edge := math.Hypot(px-nc[0], py-nc[1]) / math.Max(30, nr)
	shot := 0.0
	for _, dp := range b.dangerPts {
		if b.t-dp[2] < 40 && b.exposedTo(px, py, dp) {
			shot += 60
		}
	}
	return 120*b.L.Airiness(px, py) + 0.25*math.Hypot(px-b.pos[0], py-b.pos[1]) + 40*crowd + 10*edge + shot
}

// scoutPost: a quiet, open-but-safe waiting spot inside the next circle (40 candidates, refreshed every 12 s).
func (b *Bot) scoutPost(obs *Obs) *Vec2 {
	z := &obs.Zone
	nc, nr := z.NextCenter, z.NextRadius
	out := b.post == nil || math.Hypot(b.post[0]-nc[0], b.post[1]-nc[1]) > nr*0.8
	if out || b.t-b.postT > 12 {
		b.postT = b.t
		var best *Vec2
		bestCost := 1e18
		for i := 0; i < 40; i++ {
			a := b.rng.Float64() * 2 * math.Pi
			r := math.Sqrt(b.rng.Float64()) * nr * 0.7
			px, py := nc[0]+r*math.Cos(a), nc[1]+r*math.Sin(a)
			if !b.L.Inside(px, py) || !b.L.OnHub(px, py) {
				continue
			}
			if c := b.postPrice(obs, px, py); c < bestCost {
				best, bestCost = &Vec2{px, py}, c
			}
		}
		if best != nil && (out || b.post == nil || bestCost < b.postPrice(obs, b.post[0], b.post[1])-15) {
			b.post = best
		}
	}
	return b.post
}

// leavePocket: stranded in a walkable pocket cut off from the hub -> walk to the takeoff cell, wait for
// energy, then jump and jet to the landing cell, boosting while below the solid tops ahead.
func (b *Bot) leavePocket(me *Self) (mv Vec2, boost, jump, ok bool) {
	L := b.L
	x, y, z := b.pos[0], b.pos[1], b.pos[2]
	if b.g["esc_guard"] != 0 && b.t < b.escOffUntil {
		return
	}
	if me.Grounded {
		if z-L.FloorZ(x, y) > 3 && L.hindered[L.FineIdx(x, y)] {
			b.touchdown, b.jetting = nil, false
			return
		}
		if !L.Stray(x, y) {
			b.touchdown, b.jetting = nil, false
			return
		}
		b.jetting = false
		if b.touchdown == nil || b.t-b.escT > 6 {
			from, land, found := L.Breakout(x, y)
			b.escT = b.t
			if !found {
				b.touchdown = nil
				return
			}
			b.escFrom, b.touchdown = &from, &land
		}
		if math.Hypot(b.escFrom[0]-x, b.escFrom[1]-y) > 2.5 {
			// optional: a walk to the takeoff that makes no progress for 2 s is given up for 5 s
			// (waiting for energy at the takeoff and flying are not counted)
			if b.g["esc_guard"] != 0 {
				if !b.escTrackOn || math.Hypot(x-b.escTrack[1], y-b.escTrack[2]) > 1.5 {
					b.escTrackOn, b.escTrack = true, [3]float64{b.t, x, y}
				} else if b.t-b.escTrack[0] > 2.0 {
					b.escOffUntil = b.t + 5
					b.touchdown, b.jetting, b.escTrackOn = nil, false, false
					return
				}
			}
			return b.goTo(b.escFrom[0], b.escFrom[1]), false, false, true
		}
		b.escTrackOn = false
		if me.Energy < math.Min(70, me.MaxEnergy*0.7) {
			return Vec2{}, false, false, true
		}
		b.jetting = true
		jump = true
	} else if !b.jetting || b.touchdown == nil {
		return
	}
	lx, ly := b.touchdown[0], b.touchdown[1]
	d := math.Hypot(lx-x, ly-y)
	if d < 1.5 {
		return Vec2{}, false, jump, true
	}
	mv = Vec2{(lx - x) / d, (ly - y) / d}
	need := -1e9
	for tt := 0.0; tt <= math.Min(d, 30); tt += 1 {
		need = math.Max(need, L.CrestZ(x+mv[0]*tt, y+mv[1]*tt))
	}
	return mv, z < need+2.5 && me.Energy > 1, jump, true
}

func (b *Bot) outrunShell(obs *Obs) (Vec2, bool) {
	L := b.L
	mort := b.cfg.Weapons["mortar"]
	g := fget(mort, "gravity", 22)
	if g == 0 {
		g = 22
	}
	rad := fget(mort, "splash_radius", 7) + b.st("radius", 2.5) + 2
	for _, p := range obs.Projectiles {
		if p.Kind != "mortar" {
			continue
		}
		px, py, pz := p.Pos[0], p.Pos[1], p.Pos[2]
		vz := p.Vel[2]
		for tt := 0.0; tt < 4; tt += 0.05 {
			px += p.Vel[0] * 0.05
			py += p.Vel[1] * 0.05
			pz += vz * 0.05
			vz -= g * 0.05
			if L.IsRock(px, py, pz) {
				d := math.Hypot(px-b.pos[0], py-b.pos[1])
				if d < rad && tt < 2.5 {
					dx, dy := b.pos[0]-px, b.pos[1]-py
					if d < 0.5 {
						dx, dy = -p.Vel[1], p.Vel[0]
						d = math.Hypot(dx, dy)
						if d == 0 {
							d = 1
						}
					}
					dx, dy = dx/d, dy/d
					for _, rot := range []float64{0, 0.7, -0.7, 1.4, -1.4} {
						c, s := math.Cos(rot), math.Sin(rot)
						ex, ey := dx*c-dy*s, dx*s+dy*c
						if L.StrollOK(b.pos[0], b.pos[1], b.pos[0]+ex*6, b.pos[1]+ey*6) {
							return Vec2{ex, ey}, true
						}
					}
				}
				break
			}
		}
	}
	return Vec2{}, false
}

// ================================================================== loot
func (b *Bot) wantsGear(kind string) bool {
	if kind == "gun" {
		return b.slot(0) == nil
	}
	vals := slotValue
	if b.k["laser_first"] == 0 {
		vals = slotValuePlain
	}
	if i, _ := b.gear(kind); i >= 0 || vals[kind] <= 0 {
		return false
	}
	cur := 0.0
	if w := b.slot(1); w != nil {
		cur = vals[w.Kind]
	}
	return vals[kind] > cur
}

func (b *Bot) dropOK(obs *Obs, d *Drop) bool {
	me := b.me
	if b.phase != "hunt" || b.backing || !me.InZone || !me.Grounded || len(obs.Projectiles) > 0 {
		return false
	}
	if me.Cubes >= aget(b.cfg.Cubes, "stat_cap", 10) || b.t-b.lastHitT < 6 {
		return false
	}
	near := false
	b.enemySeen.each(func(_ int, e *Contact) {
		if b.t-e.T <= 3 && flat(e.Pos, b.pos) <= 180 {
			near = true
		}
	})
	if near || (d.Holder != nil && int(*d.Holder) != me.ID) {
		return false
	}
	eta := 1.45 * flat(d.Pos, b.pos) / b.st("walk_speed", 11.7)
	wait := math.Max(0, d.LandsIn-eta)
	capS := 4.0
	if d.CaptureS != nil {
		capS = *d.CaptureS
	}
	total := eta + wait + capS
	if eta > 15 || wait > 6 || total > 20 {
		return false
	}
	return b.t+total+3 <= b.mustLeaveBy(&obs.Zone, d.Pos[0], d.Pos[1], 8)
}

func (b *Bot) pickLoot(obs *Obs, hpf float64, danger bool) *pick {
	t := b.t
	K := b.k
	L := b.L
	z := &obs.Zone
	c, r := b.zoneNow(z)
	walk := b.st("walk_speed", 11.7)
	var best *pick
	pickUtil := 0.0
	offer := func(kind string, id int, pos Vec3, value float64) {
		if until, ok := b.banned[id]; ok && until > t {
			return
		}
		d := flat(pos, b.pos)
		if d > 260 || math.Hypot(pos[0]-c[0], pos[1]-c[1]) > r-6 {
			return
		}
		if t+d*1.4/walk+5 > b.mustLeaveBy(z, pos[0], pos[1], 6) {
			return
		}
		if pos[2]-L.FloorZ(pos[0], pos[1]) > 3.2 {
			return
		}
		if nf := L.OpenNear(L.CellAt(pos[0], pos[1])); nf < 0 || L.patch[nf] != L.hub {
			return
		}
		u := value / (d + 18)
		if id == b.chaseID {
			u *= 1.35
		}
		if u > pickUtil {
			best, pickUtil = &pick{kind, id, pos}, u
		}
	}
	b.items["pickup"].each(func(id int, p *Kept) {
		if !b.wantsGear(p.Kind) {
			return
		}
		chaseLaser := p.Kind == "laser" && K["laser_r"] > K["loot_r"]
		rad := K["loot_r"]
		if chaseLaser {
			rad = K["laser_r"]
		}
		if flat(p.Pos, b.pos) < rad {
			v := 60.0
			if b.slot(1) == nil {
				v = 130
			}
			if chaseLaser {
				v = math.Max(v, b.g["laser_v"])
			}
			offer("pickup", id, p.Pos, v)
		}
	})
	capped := b.me.Cubes >= aget(b.cfg.Cubes, "stat_cap", 10)
	cubeWorth := b.g["cube_v"]
	if capped {
		cubeWorth = 25
	}
	b.items["cube"].each(func(id int, cb *Kept) {
		if flat(cb.Pos, b.pos) < math.Max(60, K["loot_r"]*0.7) {
			offer("cube", id, cb.Pos, cubeWorth)
		}
	})
	if hpf > K["farm_hp"] && !danger && t < K["crate_until"] {
		if b.g["crate_farm"] != 0 {
			b.items["crate"].each(func(id int, cr *Kept) {
				if flat(cr.Pos, b.pos) < K["loot_r"] {
					cv := cubeWorth * 0.6
					if i, _ := b.gear("laser"); i < 0 {
						cv *= b.g["crate_nolaser"]
					}
					offer("crate", id, cr.Pos, cv)
				}
			})
		}
		if !capped && b.g["drone_farm"] != 0 {
			b.drones.each(func(id int, dr *Contact) {
				if t-dr.T < 12 && flat(dr.Pos, b.pos) < K["loot_r"] {
					offer("drone", id, dr.Pos, 85)
				}
			})
		}
	}
	if b.g["drops"] != 0 {
		for i := range obs.Drops {
			d := &obs.Drops[i]
			if !danger && flat(d.Pos, b.pos)/walk < d.LandsIn+25 && b.dropOK(obs, d) {
				offer("drop", d.ID, d.Pos, 260)
			}
		}
	}
	if best == nil {
		b.chaseID = -1
		return nil
	}
	if best.id != b.chaseID {
		b.chaseID, b.chaseT = best.id, t
	} else if t-b.chaseT > 30 {
		b.banned[best.id] = t + 60
		b.chaseID = -1
		return nil
	}
	return best
}

// ================================================================== main
func (b *Bot) Act(obs *Obs) Action {
	b.t = obs.T
	t := b.t
	me := &obs.Self
	b.me, b.pos, b.obs = me, me.Pos, obs
	act := Action{}
	b.remember(obs)
	K := b.k
	hpf := me.HP / math.Max(1, me.MaxHP)
	z := &obs.Zone
	endgame := (z.NextRadius <= 70 && !b.lateGhost) || obs.Alive <= 3

	if obs.Offer != nil && len(obs.Offer.Choices) > 0 {
		order := upgGunLine
		if K["speed_ups"] != 0 {
			order = upgSpeedLine
		} else if K["laser_ups"] != 0 {
			order = upgLaserLine
		}
		bi, bv := 0, 1<<30
		for i, ch := range obs.Offer.Choices {
			v := 99
			for j, id := range order {
				if id == ch.ID {
					v = j
					break
				}
			}
			if v < bv {
				bi, bv = i, v
			}
		}
		act.Pick = &bi
	}

	_, gun := b.gear("gun")
	_, light := b.gear("lightning")
	_, laser := b.gear("laser")

	target, vis, danger, crowd, unseen := b.appraise(obs, endgame)
	foe := target
	if foe == nil && K["peek"] != 0 {
		foe = unseen
	}
	closingKill := target != nil && target.d < 120 && (target.hp() < 200 || target.hp() < me.HP*0.5)

	// ---- when to back off
	underGunned := target != nil && target.carries("laser") && laser == nil && target.d > 55 && hpf < 0.8
	if eg := b.g["early_gunned_s"]; eg > 0 && t < eg && target != nil && target.carries("laser") && laser == nil && target.d > 30 {
		underGunned = true // early on, do not trade a gun against a laser
	}
	overrun := crowd >= 3 || (crowd == 2 && hpf < 0.65) || underGunned
	if !b.backing && ((hpf < K["pull_hp"] && t-b.dangerT < 2) || overrun) {
		b.backing = true
		b.coverT = -1 // look for cover afresh
	}
	if b.backing && !overrun && (hpf > K["steady_hp"] || (t-b.dangerT > 7 && hpf > 0.55)) {
		b.backing = false
	}
	if b.phase == "hunt" {
		near := 0
		for _, e := range vis {
			if e.T == t && e.d < 160 {
				near++
			}
		}
		behind := (near >= 2 && hpf < b.g["lose_two_hp"]) || (near >= 3 && hpf < b.g["lose_three_hp"])
		if target != nil {
			tf := target.hp() / math.Max(target.maxHP(), 1)
			if hpf < b.g["lose_low_hp"] && tf > hpf+0.1 {
				behind = true
			}
			if tf < 0.25 && hpf > 0.2 && near <= 2 {
				behind = false
			}
		}
		if behind {
			b.lostAt = t
		}
		b.backing = b.lostAt > 0 && t-b.lostAt < b.g["back_s"]
	}

	// ---- zone timing
	gx, gy := safePoint(z, b.pos[0], b.pos[1])
	zd := math.Hypot(gx-b.pos[0], gy-b.pos[1])
	need := zd*1.45/b.st("walk_speed", 11.7) + 4
	leaveBy := b.mustLeaveBy(z, b.pos[0], b.pos[1], 8)
	zoneNow := !me.InZone || t+need+3 >= leaveBy
	zoneSoon := t+need+14 >= leaveBy

	move := Vec2{}
	boost := false
	mode := "idle"
	var shootDrone *Contact
	var shootCrate *Vec3
	hopNow := false
	shellMv, shell := Vec2{}, false
	if mv, bs, jp, ok := b.leavePocket(me); ok {
		move, boost, hopNow, mode = mv, bs, jp, "escape"
	} else if t < b.jiggleEnd {
		move, mode = b.jiggleDir, "unstuck"
	} else {
		shellMv, shell = b.outrunShell(obs)
	}
	if mode != "idle" {
		// escape / unstuck already decided
	} else if shell {
		move, boost, mode = shellMv, me.Energy > 15, "shell"
	} else if zoneNow && zd > 2 {
		move, mode = b.goTo(gx, gy), "zone"
		boost = (!me.InZone || t+need >= leaveBy) && me.Energy > 5
	} else if b.backing && !closingKill && !endgame {
		if mv, ok := b.takeCover(vis); ok {
			move, mode = mv, "cover"
			boost = me.Energy > 10 && moving(mv)
		} else if target != nil {
			move, mode = b.brawlStep(obs, &target.Actor, vis, 1, 0), "cornered"
		} else {
			move, mode = b.goTo(gx, gy), "flee"
			if t-b.droneHitT < 3 && !moving(move) {
				s := 1.0 // side-step the drone, switching sides every 1.5 s
				if math.Mod(t, 3) > 1.5 {
					s = -1
				}
				move = Vec2{-math.Sin(b.droneBrg) * s, math.Cos(b.droneBrg) * s}
			}
		}
	} else if (b.phase == "scout" || b.phase == "guard") && target == nil && t-b.lastHitT < 2.5 && !endgame {
		hb := 0.0
		if b.painDir != nil {
			hb = *b.painDir
		}
		phantom := &Contact{Actor: Actor{Pos: Vec3{b.pos[0] + math.Cos(hb)*80, b.pos[1] + math.Sin(hb)*80, b.pos[2]}}}
		if mv, ok := b.takeCover(append([]*Contact{phantom}, vis...)); ok {
			move, mode = mv, "hide"
			boost = me.Energy > 30 && moving(mv)
		}
	}
	if mode == "idle" && foe != nil {
		fd := foe.d
		hotLaser := laser != nil && laser.Charge >= laser.st("charge", 0.75)-0.3 && fd <= laser.st("range", 280)-5
		armed := gun != nil && gun.ammo(0) > 0 && gun.reload() <= 0.25 && fd <= gun.st("range", 230)-10
		if hotLaser || (light != nil && fd <= light.st("range", 45)+6 && me.Energy >= light.st("energy", 3)*3) {
			armed = true
		}
		if K["peek"] == 0 && (light != nil || laser != nil) {
			armed = true
		}
		wantSight := -1.0
		if armed {
			wantSight = 1
		}
		if K["peek"] != 0 && !hotLaser {
			for _, e := range vis {
				if e.T == t && e.Charging && b.aimingAtMe(&e.Actor, 0.3) {
					wantSight = -1.6 // a laser is lined up on us: get out of its sight
				}
			}
		}
		if K["finish_r"] != 0 && foe.hp() < 0.38*foe.maxHP() && hpf > 0.42 && crowd <= 2 {
			move = b.brawlStep(obs, &foe.Actor, vis, wantSight, K["finish_r"])
		} else {
			move = b.brawlStep(obs, &foe.Actor, vis, wantSight, 0)
		}
		if light != nil && fd > 50 && me.Energy > 55 && wantSight > 0 {
			boost = true
		}
		mode = "fight"
		if fb := b.g["fight_boost"]; fb > 0 && me.Energy > fb && (laser == nil || laser.Charge <= 0) && (light == nil || me.Energy > 60) {
			boost = true
		}
		if K["cube_fight_r"] != 0 {
			rr := K["cube_fight_r"]
			if hpf <= 0.5 {
				rr *= 0.57
			}
			var cb *Kept
			b.items["cube"].each(func(_ int, c *Kept) {
				if dc := flat(c.Pos, b.pos); dc < rr {
					rr, cb = dc, c
				}
			})
			if cb != nil {
				move, mode = b.goTo(cb.Pos[0], cb.Pos[1]), "fight-cube"
			}
		}
	} else if mode == "idle" && zoneSoon && zd > 2 {
		move, mode = b.goTo(gx, gy), "zone"
	} else if mode == "idle" {
		if pk := b.pickLoot(obs, hpf, danger); pk != nil {
			mode = "loot-" + pk.kind
			d := flat(pk.pos, b.pos)
			switch pk.kind {
			case "drone":
				dr, ok := b.drones.get(pk.id)
				if ok && t-dr.T < 1 && dr.shot && dr.d < 75 {
					shootDrone = dr
					move = b.brawlStep(obs, &dr.Actor, vis, 1, 0)
				} else {
					move = b.goTo(pk.pos[0], pk.pos[1])
				}
			case "crate":
				cp := Vec3{pk.pos[0], pk.pos[1], pk.pos[2] + fget(b.cfg.Crates, "height", 2.4)*0.5}
				if d < 50 && b.L.FireLane(b.plainMuzzle(), cp) {
					shootCrate = &cp
					if d < 12 {
						move = unit(b.pos[0]-pk.pos[0], b.pos[1]-pk.pos[1])
					}
				} else {
					move = b.goTo(pk.pos[0], pk.pos[1])
				}
			default:
				move = b.goTo(pk.pos[0], pk.pos[1])
				if pk.kind == "drop" && d < 2.5 {
					move = Vec2{}
				}
			}
		} else if zd > 3 && (b.phase == "hunt" || z.NextRadius <= 30) {
			move, mode = b.goTo(gx, gy), "zone-walk"
		} else if b.phase == "scout" || b.phase == "guard" {
			if post := b.scoutPost(obs); post != nil && math.Hypot(post[0]-b.pos[0], post[1]-b.pos[1]) > 3 {
				move, mode = b.goTo(post[0], post[1]), "post"
				if !danger && t-b.lastHitT > 6 {
					q := math.Min(1, 4.6/b.st("walk_speed", 11.7)) // silent walk
					move = Vec2{move[0] * q, move[1] * q}
				}
			}
		}
	}

	// ---- dodges and stuck recovery
	if b.g["sidestep"] != 0 && mode != "unstuck" && mode != "escape" {
		if dx, dy, las := b.sidestep(obs); dx != 0 || dy != 0 {
			mx, my := move[0]*0.4+dx*0.9, move[1]*0.4+dy*0.9
			if l := math.Hypot(mx, my); l > 1 {
				mx, my = mx/l, my/l
			}
			move = Vec2{mx, my}
			if las && me.Energy > 25 {
				boost = true
			}
			if db := b.g["dodge_boost"]; db > 0 && me.Energy > db {
				boost = true
			}
		}
	}
	jump := hopNow
	if mode == "escape" {
		b.stallStart = -1
	} else if moving(move) && math.Hypot(me.Vel[0], me.Vel[1]) < 1.2 && me.Grounded {
		if b.stallStart < 0 {
			b.stallStart = t
		} else if t-b.stallStart > 0.7 {
			a := b.rng.Float64() * 2 * math.Pi
			b.jiggleDir = Vec2{math.Cos(a), math.Sin(a)}
			b.jiggleEnd = t + 0.5
			b.stallStart = -1
			b.wpathSet = false
			jump = true
		}
	} else {
		b.stallStart = -1
	}

	// ---- fire control
	aim, fire, defended := b.fire(target, shootDrone, shootCrate, vis, move, hpf)
	if target == nil && foe != nil {
		for i := 0; i < 2; i++ {
			w := b.slot(i)
			if w != nil && w.Kind == "laser" && w.Cooldown <= 0.05 && foe.d < w.st("range", 280) &&
				w.Charge < w.st("charge", 0.75)+w.st("hold_s", 2)-0.35 {
				fire[i] = true
			}
		}
	}

	// ---- reload
	if gun != nil && gun.reload() <= 0 && gun.ammo(0) < gun.st("mag", 12) {
		if target == nil && shootDrone == nil && shootCrate == nil && !defended &&
			(gun.ammo(0) <= gun.st("mag", 12)*0.6 || t-b.dangerT > 3) {
			tr := true
			act.Reload = &tr
		}
	}

	// ---- weapon pickup in reach
	for _, p := range obs.Pickups {
		if flat(p.Pos, b.pos) > aget(b.cfg.Loot, "pickup_radius", 4)-0.3 || math.Abs(p.Pos[2]-b.pos[2]) > 3.6 {
			continue
		}
		if !b.wantsGear(p.Kind) {
			continue
		}
		w1 := b.slot(1)
		if w1 != nil && b.slot(0) != nil {
			if w1.Kind == "laser" && w1.Charge > 0 {
				break
			}
			one := 1
			act.Drop = &one
		}
		id := p.ID
		act.Take = &id
		break
	}

	mvOut := [2]float64{move[0], move[1]} // full precision: rounding changes the physics over a match
	aimOut := [2]float64{aim[0], aim[1]}
	bo := boost && moving(move)
	act.Move, act.Aim, act.Fire, act.Boost, act.Jump = &mvOut, &aimOut, &fire, &bo, &jump
	b.mode = mode
	tid := -1
	if target != nil {
		tid = target.ID
	}
	act.Debug = map[string]any{"label": fmt.Sprintf("%s:%s hp%.0f c%d t%d", b.phase, mode, me.HP, int(me.Cubes), tid)}
	return act
}

// ================================================================== fire control
func leadAim(mz, tp, tv Vec3, speed float64, ground bool) (float64, float64) {
	if speed <= 0 {
		speed = 252
	}
	extra := 0.0
	var px, py, pz float64
	for i := 0; i < 4; i++ {
		tt := latency + extra
		px, py, pz = tp[0]+tv[0]*tt, tp[1]+tv[1]*tt, tp[2]
		if !ground {
			pz += tv[2] * tt * 0.5
		}
		d := math.Sqrt((px-mz[0])*(px-mz[0]) + (py-mz[1])*(py-mz[1]) + (pz-mz[2])*(pz-mz[2]))
		extra = math.Max(0, d-3) / speed
	}
	return math.Atan2(py-mz[1], px-mz[0]), math.Atan2(pz-mz[2], math.Hypot(px-mz[0], py-mz[1]))
}

func (b *Bot) fire(target *Contact, drone *Contact, crate *Vec3, vis []*Contact, move Vec2, hpf float64) (Vec2, [2]bool, bool) {
	me := b.me
	t := b.t
	var fire [2]bool
	mz := b.muzzle()
	_, gun := b.gear("gun")
	guardingDrone := false
	if target == nil && t-b.droneHitT < 3 {
		var best *Contact
		offBest := 0.7
		b.drones.each(func(_ int, dr *Contact) {
			if t-dr.T > 0.5 || !dr.shot || dr.d > 150 {
				return
			}
			off := math.Abs(wrap(math.Atan2(dr.Pos[1]-b.pos[1], dr.Pos[0]-b.pos[0]) - b.droneBrg))
			if t-fgetI(b.droneStruck, dr.ID, -99) < 3 {
				off = 0
			}
			if off < offBest {
				best, offBest = dr, off
			}
		})
		if best != nil {
			drone, crate, guardingDrone = best, nil, true
		}
	}
	var tp, tv Vec3
	have := false
	radius, height := 0.0, 0.0
	vsMech := false
	if target != nil {
		sc := b.scale(target.Cubes)
		radius, height = fget(b.cfg.Mech, "radius", 2.5)*sc, fget(b.cfg.Mech, "height", 9)*sc
		tp = Vec3{target.Pos[0], target.Pos[1], target.Pos[2] + height*0.5}
		tv = target.Vel
		if !b.L.FireLane(mz, tp) {
			tp[2] = target.Pos[2] + height*0.85
		}
		vsMech, have = true, true
	} else if drone != nil {
		radius, height = fget(b.cfg.Npc, "radius", 1.8), fget(b.cfg.Npc, "height", 3.5)
		tp = Vec3{drone.Pos[0], drone.Pos[1], drone.Pos[2] + height*0.5}
		tv = drone.Vel
		have = true
	} else if crate != nil {
		radius, height = fget(b.cfg.Crates, "radius", 1.4), fget(b.cfg.Crates, "height", 2.4)
		tp, tv, have = *crate, Vec3{}, true
	}
	if have && !vsMech && (b.backing || hpf < 0.5) && !guardingDrone {
		have = false
	}
	if !have {
		var lookAt *Contact
		for _, e := range vis {
			if e.hot || b.struckAgo(e.ID) < 6 {
				if lookAt == nil || e.d < lookAt.d { // distance as measured when last seen
					lookAt = e
				}
			}
		}
		var yaw float64
		switch {
		case lookAt != nil:
			yaw = math.Atan2(lookAt.Pos[1]-b.pos[1], lookAt.Pos[0]-b.pos[0])
		case t-b.droneHitT < 2.5:
			yaw = b.droneBrg
		case t-b.lastHitT < 2.5 && b.painDir != nil:
			yaw = *b.painDir
		case b.g["watch"] != 0 && b.attnPri > 0 && t-b.attnT < 2.5:
			yaw = b.attnBrg + 0.3*math.Sin(t*2.1)
		case b.g["watch"] == 1 && b.watchFoe() != nil:
			w := b.watchFoe()
			yaw = math.Atan2(w.Pos[1]-b.pos[1], w.Pos[0]-b.pos[0]) + 0.5*math.Sin(t*1.7)
		case moving(move):
			yaw = math.Atan2(move[1], move[0]) + 1.15*math.Sin(t*1.3)
		default:
			yaw = me.Aim[0] + 0.35
		}
		return Vec2{yaw, math.Max(b.st("min_pitch", -0.7), math.Min(b.st("max_pitch", 1.3), 0))}, fire, guardingDrone
	}

	dist := math.Hypot(tp[0]-b.pos[0], tp[1]-b.pos[1])
	onGround := math.Abs(tv[2]) < 0.5
	gyaw, gpitch := 0.0, 0.0
	inGunRange := false
	if gun != nil {
		gyaw, gpitch = leadAim(mz, tp, tv, gun.st("speed", 252), onGround)
		inGunRange = dist <= gun.st("range", 230)*0.96
	}
	hp := Vec3{tp[0] + tv[0]*(latency+0.05), tp[1] + tv[1]*(latency+0.05), tp[2]}
	hy := math.Atan2(hp[1]-mz[1], hp[0]-mz[0])
	hpitch := math.Atan2(hp[2]-mz[2], math.Hypot(hp[0]-mz[0], hp[1]-mz[1]))
	aim := Vec2{hy, hpitch}
	if gun != nil {
		aim = Vec2{gyaw, gpitch}
	}
	yawSlack := math.Atan2(radius*0.75, math.Max(dist, 1))
	pitchSlack := math.Atan2(height*0.4, math.Max(dist, 1))
	ay, ap := me.Aim[0], me.Aim[1]
	settles := func(wy, wp float64) bool {
		ey := math.Max(0, math.Abs(wrap(wy-ay))-b.st("turn_rate", 4)*0.05)
		ep := math.Max(0, math.Abs(wp-ap)-b.st("pitch_rate", 3)*0.05)
		return ey < yawSlack && ep < pitchSlack
	}
	for i := 0; i < 2; i++ {
		w := b.slot(i)
		if w == nil {
			continue
		}
		switch w.Kind {
		case "gun":
			if inGunRange && w.ammo(0) > 0 && w.reload() <= 0 && settles(gyaw, gpitch) {
				fire[i] = true
			}
		case "lightning":
			cone := w.st("cone_deg", 30) * math.Pi / 180
			if dist < w.st("range", 45)-1.5 && math.Abs(wrap(hy-ay)) < cone*0.5-0.05 && me.Energy >= w.st("energy", 3) {
				fire[i] = true
			}
		case "mortar":
			if !vsMech || w.Cooldown > 0.25 || dist < 16 || dist > 200 {
				continue
			}
			lobOrigin := Vec3{mz[0], mz[1], b.pos[2] + b.st("height", 9) + 0.5}
			feet := Vec3{tp[0], tp[1], tp[2] - height*0.5}
			if lobY, lobP, ok := b.lob(lobOrigin, feet, tv, w.st("speed", 0), w.st("gravity", 0)); ok {
				if gun == nil || gun.reload() > 0 || gun.ammo(0) == 0 || !inGunRange {
					aim = Vec2{lobY, lobP}
				}
				ey := math.Max(0, math.Abs(wrap(lobY-ay))-b.st("turn_rate", 4)*0.05)
				ep := math.Max(0, math.Abs(lobP-ap)-b.st("pitch_rate", 3)*0.05)
				if ey < 0.04 && ep < 0.03 {
					fire[i] = true
				}
			}
		case "laser":
			if !vsMech || dist > w.st("range", 280)*0.97 {
				continue
			}
			full := w.st("charge", 0.75)
			ch := w.Charge
			// release early if a partial charge already kills
			if b.g["lethal"] != 0 && b.k["lethal"] != 0 && ch >= w.st("min_charge", 0.15) && ch < full-0.051 {
				mf := w.st("min_frac", 0.35)
				dmg := w.st("damage", 315) * (mf + (1-mf)*ch/full)
				dmg *= 1 + aget(b.cfg.Cubes, "damage_per_cube", 0.04)*math.Min(me.Cubes, aget(b.cfg.Cubes, "stat_cap", 10))
				if target.carries("mortar") {
					dmg *= 1 + w.st("cookoff", 0.6)
				}
				if dmg*b.g["lethal_k"] >= target.hp() && math.Abs(wrap(hy-ay)) < yawSlack*0.9 && math.Abs(hpitch-ap) < pitchSlack {
					aim = Vec2{hy, hpitch}
					fire[i] = false
					continue
				}
			}
			if ch < full-0.051 {
				fire[i] = true
				if ch > full*0.45 {
					aim = Vec2{hy, hpitch}
				}
			} else {
				aim = Vec2{hy, hpitch}
				fire[i] = !(math.Abs(wrap(hy-ay)) < yawSlack*0.9 && math.Abs(hpitch-ap) < pitchSlack)
			}
		}
	}
	aim[1] = math.Max(b.st("min_pitch", -0.7), math.Min(b.st("max_pitch", 1.3), aim[1]))
	return aim, fire, guardingDrone
}

// lob: low-arc mortar solution from the tube to a (moving) ground point; fails if the arc meets anything solid.
func (b *Bot) lob(lobOrigin, feet, tv Vec3, v, g float64) (float64, float64, bool) {
	if v <= 0 || g <= 0 {
		return 0, 0, false
	}
	var yaw, pitch, flight float64
	for k := 0; k < 3; k++ {
		px, py := feet[0]+tv[0]*(latency+flight), feet[1]+tv[1]*(latency+flight)
		run := math.Hypot(px-lobOrigin[0], py-lobOrigin[1])
		rise := feet[2] - lobOrigin[2]
		disc := v*v*v*v - g*(g*run*run+2*rise*v*v)
		if disc < 0 || run < 1 {
			return 0, 0, false
		}
		pitch = math.Atan((v*v - math.Sqrt(disc)) / (g * run))
		yaw = math.Atan2(py-lobOrigin[1], px-lobOrigin[0])
		flight = run / (v * math.Cos(pitch))
	}
	if pitch < b.st("min_pitch", -0.7) || pitch > b.st("max_pitch", 1.3) {
		return 0, 0, false
	}
	c, sn := math.Cos(pitch), math.Sin(pitch)
	for tt := 0.1; tt < flight*0.9; tt += 0.1 {
		d := v * c * tt
		if b.L.IsRock(lobOrigin[0]+math.Cos(yaw)*d, lobOrigin[1]+math.Sin(yaw)*d, lobOrigin[2]+v*sn*tt-0.5*g*tt*tt) {
			return 0, 0, false
		}
	}
	return yaw, pitch, true
}

func fgetI(m map[int]float64, k int, d float64) float64 {
	if v, ok := m[k]; ok {
		return v
	}
	return d
}
