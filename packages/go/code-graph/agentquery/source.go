package agentquery

import (
	"bytes"
	"context"
	"fmt"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
)

const (
	maxContextLines = 200
	maxSourceBytes  = 64 << 10
)

// SourceRequest asks for the exact retained bytes of a node's span, with
// ContextLines whole lines before and after it.
type SourceRequest struct {
	RepositoryID string
	NodeID       string
	Generation   uint64
	ContextLines int
}

// SourceResult is the node's source as it was ingested. StartLine and
// EndLine cover the returned text including context; the Summary keeps the
// node's own span.
type SourceResult struct {
	Summary
	Text      string `json:"text"`
	StartLine uint32 `json:"text_start_line"`
	EndLine   uint32 `json:"text_end_line"`
	ByteStart uint64 `json:"text_byte_start"`
	ByteEnd   uint64 `json:"text_byte_end"`
	Truncated bool   `json:"truncated"` // the span exceeded the size cap and was cut
}

// NodeSource reads the retained file behind a node's anchor and cuts the
// node's span from it, verified against the content hash the node was
// projected from, so the agent sees the bytes the graph was built on.
func NodeSource(ctx context.Context, st Store, req SourceRequest) (SourceResult, error) {
	if req.ContextLines < 0 || req.ContextLines > maxContextLines {
		return SourceResult{}, fmt.Errorf("%w: context lines 0-%d", deployment.ErrInvalidRequest, maxContextLines)
	}
	v, err := st.GetNode(ctx, req.RepositoryID, req.NodeID, req.Generation)
	if err != nil {
		return SourceResult{}, err
	}
	n := v.Fact.Node
	if n.Source == nil || n.Source.ContentSHA256 == "" {
		return SourceResult{}, fmt.Errorf("%w: node %s has no source anchor", graph.ErrNotFound, req.NodeID)
	}
	data, err := st.GetSource(ctx, req.RepositoryID, n.Source.ContentSHA256)
	if err != nil {
		return SourceResult{}, err
	}
	start, end := n.Source.Span.Start.ByteOffset, n.Source.Span.End.ByteOffset
	if start > end || end > uint64(len(data)) {
		return SourceResult{}, fmt.Errorf("%w: node %s span [%d,%d) exceeds its %d-byte source", graph.ErrIntegrity, req.NodeID, start, end, len(data))
	}
	startLine, endLine := n.Source.Span.Start.Line, n.Source.Span.End.Line
	if req.ContextLines > 0 {
		// Widen to whole lines first, then add the requested lines around them.
		start = lineStart(data, start)
		if end == 0 || data[end-1] != '\n' {
			end = lineEnd(data, end)
		}
		for i := 0; i < req.ContextLines && start > 0; i++ {
			start = lineStart(data, start-1)
			startLine--
		}
		for i := 0; i < req.ContextLines && end < uint64(len(data)); i++ {
			end = lineEnd(data, end)
			endLine++
		}
	}
	result := SourceResult{Summary: Summarize(*n), StartLine: startLine, EndLine: endLine, ByteStart: start, ByteEnd: end}
	if end-start > maxSourceBytes {
		end = start + maxSourceBytes
		for end > start && data[end]&0xC0 == 0x80 {
			end--
		}
		result.Truncated = true
		result.ByteEnd = end
	}
	result.Text = string(data[start:end])
	return result, nil
}

// lineStart is the offset of the first byte of the line containing pos.
func lineStart(data []byte, pos uint64) uint64 {
	return uint64(bytes.LastIndexByte(data[:pos], '\n') + 1)
}

// lineEnd is the offset just past the newline ending the line that contains
// pos, or the end of data when the last line has no newline.
func lineEnd(data []byte, pos uint64) uint64 {
	i := bytes.IndexByte(data[pos:], '\n')
	if i < 0 {
		return uint64(len(data))
	}
	return pos + uint64(i) + 1
}
