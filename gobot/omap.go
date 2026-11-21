package main

// omap: a map that remembers first-insertion order (iteration order decides ties in target and loot picks).
type omap[T any] struct {
	keys []int
	vals map[int]T
}

func newOmap[T any]() *omap[T] { return &omap[T]{vals: map[int]T{}} }

func (m *omap[T]) set(k int, v T) {
	if _, ok := m.vals[k]; !ok {
		m.keys = append(m.keys, k)
	}
	m.vals[k] = v
}

func (m *omap[T]) get(k int) (T, bool) { v, ok := m.vals[k]; return v, ok }

func (m *omap[T]) del(k int) {
	if _, ok := m.vals[k]; !ok {
		return
	}
	delete(m.vals, k)
	for i, kk := range m.keys {
		if kk == k {
			m.keys = append(m.keys[:i], m.keys[i+1:]...)
			break
		}
	}
}

func (m *omap[T]) each(f func(k int, v T)) {
	keys := append([]int(nil), m.keys...)
	for _, k := range keys {
		if v, ok := m.vals[k]; ok {
			f(k, v)
		}
	}
}
