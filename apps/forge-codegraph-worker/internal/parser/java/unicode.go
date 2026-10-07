package java

import (
	"context"
	"unicode/utf16"
	"unicode/utf8"
)

// Tree-sitter parses the translated UTF-16 stream. Boundary offsets map its
// byte positions back to original UTF-8, including escapes that create tokens,
// comments or line breaks. UTF-16 preserves lone surrogate code units in Java
// literals; converting the stream through Go runes would lose them.
type translatedSource struct {
	units   []uint16
	offsets []uint32
	errors  []rawRange
}
type rawRange struct{ start, end uint }

func translateJava(ctx context.Context, raw []byte) (translatedSource, error) {
	s := translatedSource{units: make([]uint16, 0, len(raw)), offsets: make([]uint32, 1, len(raw)+1)}
	slashes := 0
	lastEscape := false
	nextCheck := 0
	for i := 0; i < len(raw); {
		if i >= nextCheck {
			nextCheck = i + 4096
			if err := ctx.Err(); err != nil {
				return translatedSource{}, err
			}
		}
		start := i
		eligible := lastEscape || slashes%2 == 0
		if raw[i] == '\\' && eligible && i+1 < len(raw) && raw[i+1] == 'u' {
			j := i + 1
			for j < len(raw) && raw[j] == 'u' {
				j++
				if j&4095 == 0 {
					if err := ctx.Err(); err != nil {
						return translatedSource{}, err
					}
				}
			}
			var unit uint16
			ok := j+4 <= len(raw)
			if ok {
				for k := j; k < j+4; k++ {
					h, valid := hexDigit(raw[k])
					if !valid {
						ok = false
						break
					}
					unit = unit*16 + uint16(h)
				}
			}
			if ok {
				i = j + 4
				s.units = append(s.units, unit)
				s.offsets = append(s.offsets, uint32(i))
				if unit == '\\' {
					slashes++
				} else {
					slashes = 0
				}
				lastEscape = true
				continue
			}
			end := j + 4
			if end > len(raw) {
				end = len(raw)
			}
			s.errors = append(s.errors, rawRange{uint(start), uint(end)})
		}
		r, size := utf8.DecodeRune(raw[i:])
		i += size
		if r > 0xffff {
			hi, lo := utf16.EncodeRune(r)
			s.units = append(s.units, uint16(hi), uint16(lo))
			s.offsets = append(s.offsets, uint32(start), uint32(i))
		} else {
			s.units = append(s.units, uint16(r))
			s.offsets = append(s.offsets, uint32(i))
		}
		if r == '\\' {
			slashes++
		} else {
			slashes = 0
		}
		lastEscape = false
	}
	if err := ctx.Err(); err != nil {
		return translatedSource{}, err
	}
	return s, nil
}

func hexDigit(c byte) (byte, bool) {
	switch {
	case c >= '0' && c <= '9':
		return c - '0', true
	case c >= 'a' && c <= 'f':
		return c - 'a' + 10, true
	case c >= 'A' && c <= 'F':
		return c - 'A' + 10, true
	}
	return 0, false
}
func (s translatedSource) rawOffset(native uint) uint { return uint(s.offsets[native/2]) }
func (s translatedSource) text(start, end uint) string {
	return string(utf16.Decode(s.units[start/2 : end/2]))
}
