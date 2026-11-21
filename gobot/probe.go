package main

// Developer tool (not used in matches): `gobot probe <map dir>` answers world-model queries, one JSON per
// line on stdin, so the Go world model can be checked against the Python one.

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
)

type probeQ struct {
	Q    string    `json:"q"`
	A    Vec3      `json:"a"`
	B    Vec3      `json:"b"`
	Path string    `json:"path"`
	P    []float64 `json:"p"`
}

func runProbe(mapDir string) {
	L, err := LoadLand(mapDir)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	var R router
	in := bufio.NewScanner(os.Stdin)
	in.Buffer(make([]byte, 1<<20), 1<<24)
	out := bufio.NewWriter(os.Stdout)
	defer out.Flush()
	for in.Scan() {
		var q probeQ
		if json.Unmarshal(in.Bytes(), &q) != nil {
			continue
		}
		var ans any
		switch q.Q {
		case "lane":
			ans = L.FireLane(q.A, q.B)
		case "tread":
			ans = []float64{L.TreadZ(q.A[0], q.A[1]), L.CrestZ(q.A[0], q.A[1]), L.FloorZ(q.A[0], q.A[1])}
		case "rock":
			ans = L.IsRock(q.A[0], q.A[1], q.A[2])
		case "air":
			ans = L.Airiness(q.A[0], q.A[1])
		case "stroll":
			ans = L.StrollOK(q.A[0], q.A[1], q.B[0], q.B[1])
		case "breakout":
			f, l, ok := L.Breakout(q.A[0], q.A[1])
			if ok {
				ans = []float64{f[0], f[1], l[0], l[1]}
			}
		case "route":
			ans = L.Route(&R, q.A[0], q.A[1], q.B[0], q.B[1], 22000)
		case "grid":
			f, _ := os.Create(q.Path)
			w := bufio.NewWriter(f)
			for c := range L.sealed {
				s := 0
				if L.sealed[c] {
					s = 1
				}
				fmt.Fprintf(w, "%d %.3f %.1f %d\n", s, L.standZ[c], L.margin[c], boolInt(L.patch[c] == L.hub && !L.sealed[c]))
			}
			w.Flush()
			f.Close()
			ans = []int{L.cols, L.rows}
		}
		b, _ := json.Marshal(ans)
		out.Write(b)
		out.WriteByte('\n')
		out.Flush()
	}
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}
