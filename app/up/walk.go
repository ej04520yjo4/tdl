package up

import (
	"io/fs"
	"path/filepath"
	"strings"

	"github.com/iyear/tdl/core/util/fsutil"
	"github.com/iyear/tdl/pkg/consts"
	"github.com/iyear/tdl/pkg/filterMap"
)

func walk(paths, includes, excludes []string) ([]*File, error) {
	files := make([]*File, 0)

	includesMap := extensionFilterMap(includes)
	excludesMap := extensionFilterMap(excludes)
	excludesMap[strings.ToLower(consts.UploadThumbExt)] = struct{}{} // ignore thumbnail files

	for _, path := range paths {
		err := filepath.WalkDir(path, func(path string, d fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if d.IsDir() {
				return nil
			}

			// process include and exclude
			ext := strings.ToLower(filepath.Ext(path))
			if _, ok := includesMap[ext]; len(includesMap) > 0 && !ok {
				return nil
			}
			if _, ok := excludesMap[ext]; len(excludesMap) > 0 && ok {
				return nil
			}

			f := File{File: path}
			t := strings.TrimRight(path, filepath.Ext(path)) + consts.UploadThumbExt
			if fsutil.PathExists(t) {
				f.Thumb = t
			}

			files = append(files, &f)
			return nil
		})
		if err != nil {
			return nil, err
		}
	}

	return files, nil
}

func extensionFilterMap(values []string) map[string]struct{} {
	return filterMap.New(values, func(ext string) string {
		return strings.ToLower(fsutil.AddPrefixDot(ext))
	})
}
