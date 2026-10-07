package composite

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

func digestOf(v any) string {
	data, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
