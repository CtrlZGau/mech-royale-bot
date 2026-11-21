package main

import "math"

// ------------------------------------------------------------------ route grid
func (L *Land) layGrid() {
	L.cols, L.rows = (L.fw+1)/2, (L.fh+1)/2
	n := L.cols * L.rows
	L.sealed = make([]bool, n)
	L.standZ = make([]float32, n)
	for c := 0; c < n; c++ {
		x, y := L.CellXY(c)
		z, which := L.pile(x, y, true)
		L.standZ[c] = float32(z)
		roof := which >= 0 && !L.blocks[which].bridge && z-L.FloorZ(x, y) > 1.5
		L.sealed[c] = roof || !L.fits(x, y, z, reachR)
	}
	cols, rows := L.cols, L.rows
	L.margin = make([]float32, n)
	q := make([]int32, 0, n)
	for c := 0; c < n; c++ {
		if L.sealed[c] {
			L.margin[c] = 0
			q = append(q, int32(c))
		} else {
			L.margin[c] = 1e9
		}
	}
	for k := 0; k < len(q); k++ {
		c := int(q[k])
		d := L.margin[c] + pitch
		if d > 20 {
			continue
		}
		ci, cj := c%cols, c/cols
		for _, h := range hops[:4] {
			i, j := ci+h[0], cj+h[1]
			if i >= 0 && i < cols && j >= 0 && j < rows {
				nb := j*cols + i
				if L.margin[nb] > d {
					L.margin[nb] = d
					q = append(q, int32(nb))
				}
			}
		}
	}
	for c := 0; c < n; c++ {
		if L.margin[c] > 20 {
			L.margin[c] = 20
		}
	}
	L.patch = make([]int32, n)
	var label, bestLabel int32
	bestSize := 0
	for c0 := 0; c0 < n; c0++ {
		if L.sealed[c0] || L.patch[c0] != 0 {
			continue
		}
		label++
		L.patch[c0] = label
		q = q[:0]
		q = append(q, int32(c0))
		for k := 0; k < len(q); k++ {
			c := int(q[k])
			ci, cj := c%cols, c/cols
			for _, h := range hops[:4] {
				i, j := ci+h[0], cj+h[1]
				if i >= 0 && i < cols && j >= 0 && j < rows {
					nb := j*cols + i
					if L.patch[nb] == 0 && L.CanHop(c, nb, false) && L.CanHop(nb, c, false) {
						L.patch[nb] = label
						q = append(q, int32(nb))
					}
				}
			}
		}
		if len(q) > bestSize {
			bestSize, bestLabel = len(q), label
		}
	}
	L.hub = bestLabel
}

func (L *Land) CellAt(x, y float64) int {
	ci := min(max(pyRound((x-L.ox)/pitch), 0), L.cols-1)
	cj := min(max(pyRound((y-L.oy)/pitch), 0), L.rows-1)
	return cj*L.cols + ci
}

func (L *Land) CellXY(c int) (float64, float64) {
	return L.ox + float64(c%L.cols)*pitch, L.oy + float64(c/L.cols)*pitch
}

func (L *Land) OpenAt(x, y float64) bool { return !L.sealed[L.CellAt(x, y)] }

func (L *Land) OpenNear(c int) int {
	if !L.sealed[c] {
		return c
	}
	cols, rows := L.cols, L.rows
	ci, cj := c%cols, c/cols
	best, bestD := -1, 1<<30
	for r := 1; r <= 20; r++ {
		for dj := -r; dj <= r; dj++ {
			for di := -r; di <= r; di++ {
				if max(abs(di), abs(dj)) != r {
					continue
				}
				i, j := ci+di, cj+dj
				if i >= 0 && i < cols && j >= 0 && j < rows && !L.sealed[j*cols+i] {
					d := di*di + dj*dj
					if d < bestD {
						best, bestD = j*cols+i, d
					}
				}
			}
		}
		if best >= 0 {
			return best
		}
	}
	return -1
}

func (L *Land) CanHop(a, b int, diag bool) bool {
	if L.sealed[b] {
		return false
	}
	lim := float32(2.2)
	if diag {
		lim = 3.1
	}
	return L.standZ[b]-L.standZ[a] <= lim
}

func (L *Land) StrollOK(x0, y0, x1, y1 float64) bool {
	dx, dy := x1-x0, y1-y0
	n := int(math.Hypot(dx, dy)/1.0) + 1
	prev := L.CellAt(x0, y0)
	for k := 1; k <= n; k++ {
		t := float64(k) / float64(n)
		c := L.CellAt(x0+dx*t, y0+dy*t)
		if c == prev {
			continue
		}
		if L.sealed[c] || L.standZ[c]-L.standZ[prev] > 3.1 {
			return false
		}
		prev = c
	}
	return true
}

func (L *Land) OnHub(x, y float64) bool {
	c := L.CellAt(x, y)
	return !L.sealed[c] && L.patch[c] == L.hub
}

func (L *Land) Stray(x, y float64) bool {
	c := L.OpenNear(L.CellAt(x, y))
	return c < 0 || L.patch[c] != L.hub
}

// buildHubLink: for every cell within 20 steps of the hub, the hub cell it reaches first (8-neighbour BFS).
func (L *Land) buildHubLink() {
	cols, rows := L.cols, L.rows
	n := cols * rows
	link := make([]int32, n)
	depth := make([]uint8, n)
	for i := range link {
		link[i] = -1
	}
	q := make([]int32, 0, n)
	for i := 0; i < n; i++ {
		if !L.sealed[i] && L.patch[i] == L.hub {
			link[i] = int32(i)
			q = append(q, int32(i))
		}
	}
	for k := 0; k < len(q); k++ {
		c0 := int(q[k])
		if depth[c0] >= 20 {
			continue
		}
		ci, cj := c0%cols, c0/cols
		for _, h := range hops {
			i, j := ci+h[0], cj+h[1]
			if i >= 0 && i < cols && j >= 0 && j < rows {
				nb := j*cols + i
				if link[nb] < 0 {
					link[nb] = link[c0]
					depth[nb] = depth[c0] + 1
					q = append(q, int32(nb))
				}
			}
		}
	}
	L.hubLink = link
}

// Breakout: (takeoff, landing) to get from an isolated pocket back to the hub.
func (L *Land) Breakout(x, y float64) (Vec2, Vec2, bool) {
	s := L.OpenNear(L.CellAt(x, y))
	if s < 0 {
		return Vec2{}, Vec2{}, false
	}
	cols, rows := L.cols, L.rows
	steps := map[int]int{s: 0}
	q := []int{s}
	best, bestS := -1, math.Inf(1)
	for k := 0; k < len(q) && k < 60000; k++ {
		c := q[k]
		ci, cj := c%cols, c/cols
		if m := int(L.hubLink[c]); m >= 0 {
			cx, cy := L.CellXY(c)
			mx, my := L.CellXY(m)
			sc := float64(steps[c])*pitch + 4*math.Hypot(mx-cx, my-cy)
			if sc < bestS {
				best, bestS = c, sc
			}
		}
		for hi, h := range hops {
			i, j := ci+h[0], cj+h[1]
			if i < 0 || i >= cols || j < 0 || j >= rows {
				continue
			}
			nb := j*cols + i
			if _, ok := steps[nb]; ok || !L.CanHop(c, nb, hi >= 4) {
				continue
			}
			if hi >= 4 && (L.sealed[cj*cols+i] || L.sealed[j*cols+ci]) {
				continue
			}
			steps[nb] = steps[c] + 1
			q = append(q, nb)
		}
	}
	if best < 0 {
		return Vec2{}, Vec2{}, false
	}
	lx, ly := L.CellXY(best)
	mx, my := L.CellXY(int(L.hubLink[best]))
	d := math.Hypot(mx-lx, my-ly)
	if d > 0.1 {
		ex, ey := mx+(mx-lx)/d*5, my+(my-ly)/d*5
		if L.Inside(ex, ey) && L.OnHub(ex, ey) {
			mx, my = ex, ey
		}
	}
	return Vec2{lx, ly}, Vec2{mx, my}, true
}

// Airiness: mean free sight distance (24 rays, eye height 7 m, out to 200 m) as a fraction of 200 m.
func (L *Land) Airiness(x, y float64) float64 {
	z := L.TreadZ(x, y) + 7
	total := 0.0
	for k := 0; k < 24; k++ {
		a := float64(k) * math.Pi / 12
		ca, sa := math.Cos(a), math.Sin(a)
		d := 2.5
		for d < 200 {
			px, py := x+ca*d, y+sa*d
			if !L.Inside(px, py) || L.rockCached(px, py, z) {
				break
			}
			d += 2.5
		}
		total += d
	}
	return total / (24 * 200)
}

// ------------------------------------------------------------------ A* (whole search in one call)
type heapItem struct {
	f float64
	c int32
}

type minHeap []heapItem

func (h minHeap) less(a, b int) bool {
	return h[a].f < h[b].f || (h[a].f == h[b].f && h[a].c < h[b].c)
}

func (h *minHeap) push(it heapItem) {
	*h = append(*h, it)
	s := *h
	i := len(s) - 1
	for i > 0 {
		p := (i - 1) / 2
		if !s.less(i, p) {
			break
		}
		s[i], s[p] = s[p], s[i]
		i = p
	}
}

func (h *minHeap) pop() heapItem {
	s := *h
	top := s[0]
	last := len(s) - 1
	s[0] = s[last]
	s = s[:last]
	i := 0
	for {
		l, r, m := 2*i+1, 2*i+2, i
		if l < len(s) && s.less(l, m) {
			m = l
		}
		if r < len(s) && s.less(r, m) {
			m = r
		}
		if m == i {
			break
		}
		s[i], s[m] = s[m], s[i]
		i = m
	}
	*h = s
	return top
}

type router struct {
	cost  []float64
	came  []int32
	stamp []uint32
	shut  []uint32
	gen   uint32
	heap  minHeap
}

// Route: cells from start to goal (or to the closest reached cell when the goal is out of reach).
func (L *Land) Route(R *router, sx, sy, gx, gy float64, maxExpand int) []int {
	s := L.OpenNear(L.CellAt(sx, sy))
	g := L.OpenNear(L.CellAt(gx, gy))
	if s < 0 || g < 0 {
		return nil
	}
	if s == g {
		return []int{s}
	}
	cols, rows := L.cols, L.rows
	n := cols * rows
	if len(R.cost) != n {
		R.cost = make([]float64, n)
		R.came = make([]int32, n)
		R.stamp = make([]uint32, n)
		R.shut = make([]uint32, n)
	}
	R.gen++
	gen := R.gen
	gi, gj := g%cols, g/cols
	h := func(c int) float64 {
		dx, dy := abs(c%cols-gi), abs(c/cols-gj)
		mn, mx := min(dx, dy), max(dx, dy)
		return (float64(mx) + 0.4142*float64(mn)) * pitch * 1.15
	}
	R.heap = R.heap[:0]
	R.cost[s], R.came[s], R.stamp[s] = 0, -1, gen
	R.heap.push(heapItem{h(s), int32(s)})
	best, bestH := s, h(s)
	expanded := 0
	for len(R.heap) > 0 && expanded < maxExpand {
		c := int(R.heap.pop().c)
		if R.shut[c] == gen {
			continue
		}
		R.shut[c] = gen
		expanded++
		if c == g {
			best = c
			break
		}
		if hc := h(c); hc < bestH {
			best, bestH = c, hc
		}
		ci, cj := c%cols, c/cols
		for hi, hp := range hops {
			i, j := ci+hp[0], cj+hp[1]
			if i < 0 || i >= cols || j < 0 || j >= rows {
				continue
			}
			nb := j*cols + i
			diag := hi >= 4
			if R.shut[nb] == gen || !L.CanHop(c, nb, diag) {
				continue
			}
			step := pitch
			if diag {
				if L.sealed[cj*cols+i] || L.sealed[j*cols+ci] {
					continue
				}
				step *= 1.4142
			}
			if cl := float64(L.margin[nb]); cl < 4 {
				step *= 1 + (4-cl)*0.15
			}
			ng := R.cost[c] + step
			if R.stamp[nb] != gen || ng < R.cost[nb] {
				R.stamp[nb] = gen
				R.cost[nb] = ng
				R.came[nb] = int32(c)
				R.heap.push(heapItem{ng + h(nb), int32(nb)})
			}
		}
	}
	var path []int
	for c := best; c >= 0; {
		path = append(path, c)
		if c == s {
			break
		}
		if R.stamp[c] != gen {
			break
		}
		c = int(R.came[c])
	}
	for i, j := 0, len(path)-1; i < j; i, j = i+1, j-1 {
		path[i], path[j] = path[j], path[i]
	}
	return path
}

func abs(v int) int {
	if v < 0 {
		return -v
	}
	return v
}
