package store

import (
	"strings"
	"sync"
	"testing"
)

func TestNextIDIsUniqueUnderConcurrency(t *testing.T) {
	const workers = 64
	const idsPerWorker = 200

	ids := make(chan string, workers*idsPerWorker)
	var group sync.WaitGroup
	for range workers {
		group.Add(1)
		go func() {
			defer group.Done()
			for range idsPerWorker {
				ids <- nextID("stress")
			}
		}()
	}
	group.Wait()
	close(ids)

	seen := make(map[string]struct{}, workers*idsPerWorker)
	for id := range ids {
		if !strings.HasPrefix(id, "stress_") {
			t.Fatalf("unexpected ID prefix: %q", id)
		}
		if _, exists := seen[id]; exists {
			t.Fatalf("duplicate ID generated: %q", id)
		}
		seen[id] = struct{}{}
	}
	if len(seen) != workers*idsPerWorker {
		t.Fatalf("generated %d unique IDs, want %d", len(seen), workers*idsPerWorker)
	}
}
