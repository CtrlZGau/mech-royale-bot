package main

// Land: the world model used for movement and shooting decisions.
//
// Fine layers (1 m raster from the terrain grid and the prism footprints):
//   floor    terrain height
//   crest    top of the solid stack on the floor (solids starting <= 1.5 m above the stack join it)
//   tread    walking height (crest, or a low bridge deck)
//   hindered stack rises > 1.2 m above the floor, or a low overhead solid
//   sheets   solids floating above the stack (overhangs, high bridges)
// Route grid (2 m cells at minX + 2i): sealed when a roof, or when a mech of radius 2.6 cannot stand there
// without a wall inside it; margin = metres to the nearest sealed cell (<= 20); patches = 4-connected
// walkable areas (hub = the largest).
// Line of fire: segment against prism faces/caps (16 m bins) and terrain (ray march, 8 m block-max skip).

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
)

const (
	binSize = 16.0
	stepUp  = 1.2
	reachR  = 2.6
	pitch   = 2.0
)

var hops = [8][2]int{{1, 0}, {-1, 0}, {0, 1}, {0, -1}, {1, 1}, {1, -1}, {-1, 1}, {-1, -1}}

type block struct {
	bridge         bool
	base, top      float64
	lines          []float64 // segments x0 y0 x1 y1 ...
	x0, y0, x1, y1 float64
}

type sheet struct{ base, top float32 }

type Land struct {
	ox, oy             float64
	fw, fh             int
	ex0, ex1, ey0, ey1 float64
	floor              []float32
	blocks             []block

	bx0, by0     float64
	bnx, bny     int
	binBlocks    [][]int32
	cnx, cny     int
	landCap      []float64
	crest, tread []float32
	hindered     []bool
	sheets       map[int32][]sheet

	cols, rows int
	sealed     []bool
	standZ     []float32
	margin     []float32
	patch      []int32
	hub        int32
	hubLink    []int32
}

func insidePoly(lines []float64, x, y float64) bool {
	odd := false
	for k := 0; k+3 < len(lines); k += 4 {
		ax, ay, bx, by := lines[k], lines[k+1], lines[k+2], lines[k+3]
		if (ay > y) != (by > y) && x < (bx-ax)*(y-ay)/(by-ay)+ax {
			odd = !odd
		}
	}
	return odd
}

type worldFile struct {
	Terrain struct {
		File string  `json:"file"`
		MinX float64 `json:"minX"`
		MinY float64 `json:"minY"`
		Nx   int     `json:"nx"`
		Ny   int     `json:"ny"`
	} `json:"terrain"`
	Bounds struct {
		MinX float64 `json:"minX"`
		MaxX float64 `json:"maxX"`
		MinY float64 `json:"minY"`
		MaxY float64 `json:"maxY"`
	} `json:"bounds"`
	Prisms []struct {
		Kind  string      `json:"kind"`
		Base  float64     `json:"base"`
		Top   float64     `json:"top"`
		Ring  []float64   `json:"ring"`
		Holes [][]float64 `json:"holes"`
	} `json:"prisms"`
}

// FlatLand: the arena without terrain or buildings (used only when the map files cannot be read).
func FlatLand() *Land {
	L := &Land{ox: -513, oy: -334, fw: 1070, fh: 724, ex0: -512, ex1: 555, ey0: -333, ey1: 388}
	L.floor = make([]float32, L.fw*L.fh)
	L.makeBins()
	L.makeLandCaps()
	L.makeLayers(&worldFile{})
	L.layGrid()
	L.buildHubLink()
	return L
}

func LoadLand(mapDir string) (*Land, error) {
	raw, err := os.ReadFile(filepath.Join(mapDir, "v2", "world.json"))
	if err != nil {
		return nil, err
	}
	var wf worldFile
	if err := json.Unmarshal(raw, &wf); err != nil {
		return nil, err
	}
	L := &Land{ox: wf.Terrain.MinX, oy: wf.Terrain.MinY, fw: wf.Terrain.Nx, fh: wf.Terrain.Ny,
		ex0: wf.Bounds.MinX, ex1: wf.Bounds.MaxX, ey0: wf.Bounds.MinY, ey1: wf.Bounds.MaxY}
	tf := wf.Terrain.File
	if tf == "" {
		tf = "terrain.i16"
	}
	hb, err := os.ReadFile(filepath.Join(mapDir, "v2", tf))
	if err != nil {
		return nil, err
	}
	n := L.fw * L.fh
	if n == 0 || len(hb) < 2*n {
		return nil, fmt.Errorf("terrain file is %d bytes, expected %d", len(hb), 2*n)
	}
	L.floor = make([]float32, n)
	for i := 0; i < n && 2*i+1 < len(hb); i++ {
		L.floor[i] = float32(int16(binary.LittleEndian.Uint16(hb[2*i:]))) * 0.01
	}
	type rings struct {
		all [][]float64
	}
	var prismRings []rings
	for _, p := range wf.Prisms {
		rs := [][]float64{p.Ring}
		rs = append(rs, p.Holes...)
		var lines []float64
		for _, r := range rs {
			m := len(r) / 2
			for a := 0; a < m; a++ {
				c := (a + 1) % m
				lines = append(lines, r[2*a], r[2*a+1], r[2*c], r[2*c+1])
			}
		}
		prismRings = append(prismRings, rings{rs})
		if len(lines) == 0 {
			continue
		}
		b := block{bridge: p.Kind == "bridge", base: p.Base, top: p.Top, lines: lines,
			x0: math.Inf(1), y0: math.Inf(1), x1: math.Inf(-1), y1: math.Inf(-1)}
		for k := 0; k < len(lines); k += 2 {
			b.x0 = math.Min(b.x0, lines[k])
			b.x1 = math.Max(b.x1, lines[k])
			b.y0 = math.Min(b.y0, lines[k+1])
			b.y1 = math.Max(b.y1, lines[k+1])
		}
		L.blocks = append(L.blocks, b)
	}
	L.makeBins()
	L.makeLandCaps()
	L.makeLayers(&wf)
	L.layGrid()
	L.buildHubLink()
	return L, nil
}

// ------------------------------------------------------------------ bins
func (L *Land) makeBins() {
	L.bx0, L.by0 = L.ex0-binSize, L.ey0-binSize
	L.bnx = int(math.Ceil((L.ex1-L.ex0)/binSize)) + 3
	L.bny = int(math.Ceil((L.ey1-L.ey0)/binSize)) + 3
	L.binBlocks = make([][]int32, L.bnx*L.bny)
	for i := range L.blocks {
		b := &L.blocks[i]
		L.eachBin(b.x0, b.y0, b.x1, b.y1, func(c int) {
			L.binBlocks[c] = append(L.binBlocks[c], int32(i))
		})
	}
}

func (L *Land) binOf(x, y float64) int {
	i := int(math.Floor((x - L.bx0) / binSize))
	j := int(math.Floor((y - L.by0) / binSize))
	if i < 0 || j < 0 || i >= L.bnx || j >= L.bny {
		return -1
	}
	return j*L.bnx + i
}

func (L *Land) eachBin(x0, y0, x1, y1 float64, f func(int)) {
	i0 := max(0, int(math.Floor((x0-L.bx0)/binSize)))
	i1 := min(L.bnx-1, int(math.Floor((x1-L.bx0)/binSize)))
	j0 := max(0, int(math.Floor((y0-L.by0)/binSize)))
	j1 := min(L.bny-1, int(math.Floor((y1-L.by0)/binSize)))
	for j := j0; j <= j1; j++ {
		for i := i0; i <= i1; i++ {
			f(j*L.bnx + i)
		}
	}
}

// traceBins visits the bins crossed by the segment in order (2D DDA); f returns false to stop.
func (L *Land) traceBins(x0, y0, x1, y1 float64, f func(int) bool) {
	dx, dy := x1-x0, y1-y0
	ci := int(math.Floor((x0 - L.bx0) / binSize))
	cj := int(math.Floor((y0 - L.by0) / binSize))
	ei := int(math.Floor((x1 - L.bx0) / binSize))
	ej := int(math.Floor((y1 - L.by0) / binSize))
	si, sj := -1, -1
	if dx > 0 {
		si = 1
	}
	if dy > 0 {
		sj = 1
	}
	inf := math.Inf(1)
	tdx, tdy, tmx, tmy := inf, inf, inf, inf
	if math.Abs(dx) > 1e-9 {
		tdx = binSize / math.Abs(dx)
		if dx > 0 {
			tmx = (float64(ci+1)*binSize + L.bx0 - x0) / math.Abs(dx)
		} else {
			tmx = (x0 - (float64(ci)*binSize + L.bx0)) / math.Abs(dx)
		}
	}
	if math.Abs(dy) > 1e-9 {
		tdy = binSize / math.Abs(dy)
		if dy > 0 {
			tmy = (float64(cj+1)*binSize + L.by0 - y0) / math.Abs(dy)
		} else {
			tmy = (y0 - (float64(cj)*binSize + L.by0)) / math.Abs(dy)
		}
	}
	for it := 0; it < 4096; it++ {
		if ci >= 0 && ci < L.bnx && cj >= 0 && cj < L.bny {
			if !f(cj*L.bnx + ci) {
				return
			}
		}
		if ci == ei && cj == ej {
			return
		}
		var t float64
		if tmx < tmy {
			t = tmx
			tmx += tdx
			ci += si
		} else {
			t = tmy
			tmy += tdy
			cj += sj
		}
		if t > 1 {
			return
		}
	}
}

// ------------------------------------------------------------------ terrain
func (L *Land) makeLandCaps() {
	fw, fh := L.fw, L.fh
	L.cnx, L.cny = (fw+7)/8, (fh+7)/8
	bm := make([]float64, L.cnx*L.cny)
	for i := range bm {
		bm[i] = -1e30
	}
	for j := 0; j < fh; j++ {
		for i := 0; i < fw; i++ {
			v := float64(L.floor[j*fw+i])
			k := (j/8)*L.cnx + i/8
			if v > bm[k] {
				bm[k] = v
			}
		}
	}
	L.landCap = make([]float64, len(bm))
	for j := 0; j < L.cny; j++ {
		for i := 0; i < L.cnx; i++ {
			v := bm[j*L.cnx+i]
			if i+1 < L.cnx {
				v = math.Max(v, bm[j*L.cnx+i+1])
			}
			if j+1 < L.cny {
				v = math.Max(v, bm[(j+1)*L.cnx+i])
			}
			if i+1 < L.cnx && j+1 < L.cny {
				v = math.Max(v, bm[(j+1)*L.cnx+i+1])
			}
			L.landCap[j*L.cnx+i] = v
		}
	}
}

func (L *Land) FloorZ(x, y float64) float64 {
	fx := math.Min(math.Max(x-L.ox, 0), float64(L.fw-1)-1e-6)
	fy := math.Min(math.Max(y-L.oy, 0), float64(L.fh-1)-1e-6)
	i, j := int(fx), int(fy)
	tx, ty := fx-float64(i), fy-float64(j)
	fw := L.fw
	a, b := float64(L.floor[j*fw+i]), float64(L.floor[j*fw+i+1])
	c, d := float64(L.floor[(j+1)*fw+i]), float64(L.floor[(j+1)*fw+i+1])
	return (a*(1-tx)+b*tx)*(1-ty) + (c*(1-tx)+d*tx)*ty
}

// ------------------------------------------------------------------ fine layers
type piece struct {
	base, top float32
	bridge    bool
}

func (L *Land) makeLayers(wf *worldFile) {
	fw, fh, ox, oy := L.fw, L.fh, L.ox, L.oy
	spans := map[int32][]piece{}
	xs := make([]float64, 0, 64)
	for _, p := range wf.Prisms {
		rs := [][]float64{p.Ring}
		rs = append(rs, p.Holes...)
		if len(p.Ring) < 6 {
			continue
		}
		ymin, ymax := math.Inf(1), math.Inf(-1)
		for k := 1; k < len(p.Ring); k += 2 {
			ymin = math.Min(ymin, p.Ring[k])
			ymax = math.Max(ymax, p.Ring[k])
		}
		j0 := int(math.Ceil(ymin - oy))
		j1 := int(math.Floor(ymax - oy))
		pc := piece{float32(p.Base), float32(p.Top), p.Kind == "bridge"}
		for j := max(j0, 0); j <= min(j1, fh-1); j++ {
			y := oy + float64(j) + 1e-4
			xs = xs[:0]
			for _, r := range rs {
				m := len(r) / 2
				for a := 0; a < m; a++ {
					c := (a + 1) % m
					ax, ay, bx, by := r[2*a], r[2*a+1], r[2*c], r[2*c+1]
					if (ay <= y) != (by <= y) {
						xs = append(xs, ax+(y-ay)/(by-ay)*(bx-ax))
					}
				}
			}
			sort.Float64s(xs)
			for k := 0; k+1 < len(xs); k += 2 {
				i0 := int(math.Ceil(xs[k] - ox))
				i1 := int(math.Floor(xs[k+1] - ox))
				for i := max(i0, 0); i <= min(i1, fw-1); i++ {
					idx := int32(j*fw + i)
					spans[idx] = append(spans[idx], pc)
				}
			}
		}
	}
	n := fw * fh
	L.crest = make([]float32, n)
	L.tread = make([]float32, n)
	copy(L.crest, L.floor)
	copy(L.tread, L.floor)
	L.hindered = make([]bool, n)
	L.sheets = map[int32][]sheet{}
	for idx, pieces := range spans {
		sort.SliceStable(pieces, func(a, b int) bool { return pieces[a].base < pieces[b].base })
		g := L.floor[idx]
		top := g
		deck := float32(-1e9)
		low := false
		for _, pc := range pieces {
			if pc.top <= top {
				continue
			}
			if pc.base <= top+1.5 {
				top = pc.top
				continue
			}
			L.sheets[idx] = append(L.sheets[idx], sheet{pc.base, pc.top})
			if pc.base-top < 10.5 {
				if pc.bridge && pc.top-g < 6.5 {
					deck = pc.top
				} else {
					low = true
				}
			}
		}
		L.crest[idx] = top
		if deck > top {
			L.tread[idx] = deck
		} else {
			L.tread[idx] = top
		}
		L.hindered[idx] = top-g > 1.2 || low
	}
}

func nearest(v float64) int { return int(math.Round(v)) }

func (L *Land) FineIdx(x, y float64) int {
	i := min(max(nearest(x-L.ox), 0), L.fw-1)
	j := min(max(nearest(y-L.oy), 0), L.fh-1)
	return j*L.fw + i
}

func (L *Land) Inside(x, y float64) bool {
	return L.ex0 < x && x < L.ex1 && L.ey0 < y && y < L.ey1
}

// pile: height and block index of the solid stack under (x, y); deck = also step onto low bridge decks.
func (L *Land) pile(x, y float64, deck bool) (float64, int) {
	g := L.FloorZ(x, y)
	z, which := g, -1
	c := L.binOf(x, y)
	if c < 0 {
		return z, which
	}
	ids := L.binBlocks[c]
	for it := 0; it < 8; it++ {
		moved := false
		for _, bi := range ids {
			b := &L.blocks[bi]
			if b.top <= z || x < b.x0 || x > b.x1 || y < b.y0 || y > b.y1 {
				continue
			}
			if !(b.base <= z+1.5 || (deck && b.bridge && b.top-g < 6.5)) {
				continue
			}
			if insidePoly(b.lines, x, y) {
				z, which = b.top, int(bi)
				moved = true
			}
		}
		if !moved {
			break
		}
	}
	return z, which
}

func (L *Land) TreadZ(x, y float64) float64 { z, _ := L.pile(x, y, true); return z }
func (L *Land) CrestZ(x, y float64) float64 { z, _ := L.pile(x, y, false); return z }

func (L *Land) IsRock(x, y, z float64) bool {
	if z < L.FloorZ(x, y) {
		return true
	}
	c := L.binOf(x, y)
	if c < 0 {
		return false
	}
	for _, bi := range L.binBlocks[c] {
		b := &L.blocks[bi]
		if b.base < z && z < b.top && b.x0 <= x && x <= b.x1 && b.y0 <= y && y <= b.y1 && insidePoly(b.lines, x, y) {
			return true
		}
	}
	return false
}

func (L *Land) rockCached(x, y, z float64) bool {
	idx := L.FineIdx(x, y)
	if z < float64(L.crest[idx])-0.05 {
		return true
	}
	for _, s := range L.sheets[int32(idx)] {
		if float64(s.base) < z && z < float64(s.top) {
			return true
		}
	}
	return false
}

func (L *Land) wallNear(x, y, z, r, h float64) bool {
	lo, hi := z+stepUp, z+h
	r2 := r * r
	found := false
	L.eachBin(x-r, y-r, x+r, y+r, func(c int) {
		if found {
			return
		}
		for _, bi := range L.binBlocks[c] {
			b := &L.blocks[bi]
			if b.top <= lo || b.base >= hi || x+r < b.x0 || x-r > b.x1 || y+r < b.y0 || y-r > b.y1 {
				continue
			}
			e := b.lines
			for k := 0; k < len(e); k += 4 {
				x0, y0 := e[k], e[k+1]
				ux, uy := e[k+2]-x0, e[k+3]-y0
				l2 := ux*ux + uy*uy
				tt := 0.0
				if l2 > 1e-9 {
					tt = math.Max(0, math.Min(1, ((x-x0)*ux+(y-y0)*uy)/l2))
				}
				mx, my := x0+ux*tt-x, y0+uy*tt-y
				if mx*mx+my*my < r2 {
					found = true
					return
				}
			}
			if insidePoly(e, x, y) {
				found = true
				return
			}
		}
	})
	return found
}

func (L *Land) fits(x, y, z, r float64) bool {
	if x < L.ex0+r || x > L.ex1-r || y < L.ey0+r || y > L.ey1-r {
		return false
	}
	return !L.wallNear(x, y, z, r, 9)
}

// ------------------------------------------------------------------ line of fire
func (L *Land) hitBlock(bi int32, x, y, z, dx, dy, dz, tmax float64) bool {
	b := &L.blocks[bi]
	t0, t1 := 0.0, tmax
	axes := [3][4]float64{{x, dx, b.x0, b.x1}, {y, dy, b.y0, b.y1}, {z, dz, b.base, b.top}}
	for _, ax := range axes {
		o, d, lo, hi := ax[0], ax[1], ax[2], ax[3]
		if math.Abs(d) < 1e-9 {
			if !(lo <= o && o <= hi) {
				return false
			}
			continue
		}
		a, c := (lo-o)/d, (hi-o)/d
		if a > c {
			a, c = c, a
		}
		t0, t1 = math.Max(t0, a), math.Min(t1, c)
		if t0 > t1 {
			return false
		}
	}
	e := b.lines
	if b.base <= z && z <= b.top && insidePoly(e, x, y) {
		return true
	}
	best := tmax
	for k := 0; k < len(e); k += 4 {
		ex, ey := e[k+2]-e[k], e[k+3]-e[k+1]
		den := dx*ey - dy*ex
		if math.Abs(den) < 1e-9 {
			continue
		}
		px, py := e[k]-x, e[k+1]-y
		t := (px*ey - py*ex) / den
		s := (px*dy - py*dx) / den
		if t < 0 || t >= best || s < 0 || s > 1 {
			continue
		}
		hz := z + dz*t
		if hz < b.base || hz > b.top {
			continue
		}
		return true
	}
	if math.Abs(dz) > 1e-9 {
		for _, cap := range [2]struct {
			cz float64
			up bool
		}{{b.top, true}, {b.base, false}} {
			t := (cap.cz - z) / dz
			if t < 0 || t >= best || (cap.up && dz > 0) || (!cap.up && dz < 0) {
				continue
			}
			if insidePoly(e, x+dx*t, y+dy*t) {
				return true
			}
		}
	}
	return false
}

func (L *Land) hitGround(x, y, z, x1, y1, z1, tmax float64) bool {
	dx, dy, dz := x1-x, y1-y, z1-z
	hl := math.Hypot(dx, dy)
	if z-L.FloorZ(x, y) < 0 {
		return true
	}
	step := 1.0
	if hl > 1e-9 {
		step = 0.5 / hl
	}
	t := 0.0
	for it := 0; t < tmax; it++ {
		if it > 100000 {
			return false
		}
		px, py := x+dx*t, y+dy*t
		bi := int(math.Floor((px - L.ox) / 8.0))
		bj := int(math.Floor((py - L.oy) / 8.0))
		inb := bi >= 0 && bi < L.cnx && bj >= 0 && bj < L.cny
		tb := tmax
		if hl > 1e-9 {
			ex, ey := L.ox+float64(bi)*8.0, L.oy+float64(bj)*8.0
			nx, ny := math.Inf(1), math.Inf(1)
			if dx > 1e-9 {
				nx = (ex + 8.0 - px) / dx
			} else if dx < -1e-9 {
				nx = (ex - px) / dx
			}
			if dy > 1e-9 {
				ny = (ey + 8.0 - py) / dy
			} else if dy < -1e-9 {
				ny = (ey - py) / dy
			}
			tb = math.Min(t+math.Max(0, math.Min(nx, ny))+1e-6, tmax)
		}
		if inb && math.Min(z+dz*t, z+dz*tb) > L.landCap[bj*L.cnx+bi] {
			t = tb
			continue
		}
		s := math.Min(t+step, tmax)
		if z+dz*s-L.FloorZ(x+dx*s, y+dy*s) < 0 {
			return true
		}
		if s >= tmax {
			break
		}
		t = s
	}
	return false
}

// FireLane: true when the straight segment a->b meets no prism and no terrain (trees never stop shots).
func (L *Land) FireLane(a, b Vec3) bool {
	x, y, z := a[0], a[1], a[2]
	dx, dy, dz := b[0]-x, b[1]-y, b[2]-z
	blocked := false
	var tried [64]int32
	nt := 0
	var triedMap map[int32]bool
	L.traceBins(x, y, b[0], b[1], func(c int) bool {
		for _, bi := range L.binBlocks[c] {
			seen := false
			if triedMap != nil {
				seen = triedMap[bi]
			} else {
				for k := 0; k < nt; k++ {
					if tried[k] == bi {
						seen = true
						break
					}
				}
			}
			if seen {
				continue
			}
			if triedMap == nil && nt < len(tried) {
				tried[nt] = bi
				nt++
			} else {
				if triedMap == nil {
					triedMap = map[int32]bool{}
					for k := 0; k < nt; k++ {
						triedMap[tried[k]] = true
					}
				}
				triedMap[bi] = true
			}
			if L.hitBlock(bi, x, y, z, dx, dy, dz, 1.0) {
				blocked = true
				return false
			}
		}
		return true
	})
	if blocked {
		return false
	}
	return !L.hitGround(x, y, z, b[0], b[1], b[2], 1.0)
}
