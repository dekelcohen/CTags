"""
Rank and Filter support for ctags plugin for Sublime Text 2/3.
"""

from functools import reduce
import sys
import os
import re
import string


from helpers.common import *


def compile_definition_filters(view):
    filters = []
    for selector, regexes in list(
            get_setting('definition_filters', {}).items()):
        if view.match_selector(view.sel() and view.sel()[0].begin() or 0,
                               selector):
            filters.append(regexes)
    return filters


def get_grams(str):
    """
    Return a set of tri-grams (each tri-gram is a tuple) given a string:
    Ex: 'Dekel' --> {('d', 'e', 'k'), ('k', 'e', 'l'), ('e', 'k', 'e')}
    """
    lstr = str.lower()
    return set(zip(lstr, lstr[1:], lstr[2:]))


class RankMgr:
    """
    For each matched Tag, calculates the rank score or filter it out. The remaining matches are sorted by decending score.
    """

    def __init__(self, region, mbrParts, view, symbol, sym_line):
        self.region = region
        self.mbrParts = mbrParts
        self.view = view
        # Used by Rank by Definition Types
        self.symbol = symbol
        self.sym_line = sym_line

        self.lang = get_lang_setting(get_source(view))
        self.mbr_exp = self.lang.get('member_exp', {})

        self.def_filters = compile_definition_filters(view)

        self.fname_abs = view.file_name().lower() if not(
            view.file_name() is None) else None

        mbrGrams = [get_grams(part) for part in mbrParts]
        self.setMbrGrams = (
            reduce(
                lambda s,
                t: s.union(t),
                mbrGrams) if mbrGrams else set())

    def pass_def_filter(self, o):
        for f in self.def_filters:
            for k, v in list(f.items()):
                if k in o:
                    if re.match(v, o[k]):
                        return False
        return True

    def eq_filename(self, rel_path):
        if self.fname_abs is None or rel_path is None:
            return False
        return self.fname_abs.endswith(rel_path.lstrip('.').lower())

    def scope_filter(self, taglist):
        """
        Given optional scope extended field tag.scope = 'startline:startcol-endline:endcol' -  def-scope.
        Return: Tuple of 2 Lists:
        in_scope: Tags with matching scope: current cursor / caret position is contained in their start-end scope range.
        no_scope: Tags without scope or with global scope
        Usage: locals, local parameters Tags have scope (ex: in estr.js tag generator for JavaScript)
        """
        in_scope = []
        no_scope = []
        for tag in taglist:
            if self.region is None or tag.get(
                    'scope') is None or tag.scope is None or tag.scope == 'global':
                no_scope.append(tag)
                continue

            if not self.eq_filename(tag.filename):
                continue

            mch = re.search(get_setting('scope_re'), tag.scope)

            if mch:
                # .tags file is 1 based and region.begin() is 0 based
                beginLine = int(mch.group(1)) - 1
                beginCol = int(mch.group(2)) - 1
                endLine = int(mch.group(3)) - 1
                endCol = int(mch.group(4)) - 1
                beginPoint = self.view.text_point(beginLine, beginCol)
                endPoint = self.view.text_point(endLine, endCol)
                if self.region.begin() >= beginPoint and self.region.end() <= endPoint:
                    in_scope.append(tag)

        return (in_scope, no_scope)

    RANK_MATCH_TYPE = 60
    tag_types = None

    def get_type_rank(self, tag):
        """
        Rank by Definition Types: Rank Higher matching definitions with types matching to the GotoDef <reference>
        Use regex to identify the <reference> type
        """
        # First time - compare current symbol line to the per-language list of regex: Each regex is mapped to 1 or more tag types
        # Try all regex to build a list of preferred / higher rank tag types
        if self.tag_types is None:
            self.tag_types = set()
            reference_types = self.lang.get('reference_types', {})
            for re_ref, lstTypes in reference_types.items():
                # replace special keyword __symbol__ with our reference symbol
                cur_re = re_ref.replace('__symbol__', self.symbol)
                if (re.search(cur_re, self.sym_line)):
                    self.tag_types = self.tag_types.union(lstTypes)

        return self.RANK_MATCH_TYPE if tag.type in self.tag_types else 0

    RANK_EQ_FILENAME_RANK = 10
    reThis = None

    def get_samefile_rank(self, rel_path, mbrParts):
        """
        If both reference and definition (tag) are in the same file --> Rank this tag higher.
        Tag from same file as reference --> Boost rank
        Tag from same file as reference and this|self.method() --> Double boost rank
        Note: Inheritence model (base class in different file) is not yet supported.
        """
        if self.reThis is None:
            lstThis = self.mbr_exp.get('this')
            if lstThis:
                self.reThis = re.compile(concat_re(lstThis), re.IGNORECASE)
            elif self.mbr_exp:
                print(
                    'Warning! Language that has syntax settings is expected to define this|self expression syntax')

        rank = 0
        if self.eq_filename(rel_path):
            rank += self.RANK_EQ_FILENAME_RANK
            if len(mbrParts) == 1 and self.reThis and self.reThis.match(
                    mbrParts[-1]):
                # this.mtd() -  rank candidate from current file very high.
                rank += self.RANK_EQ_FILENAME_RANK
        return rank

    RANK_EXACT_MATCH_RIGHTMOST_MBR_PART_TO_FILENAME = 20
    WEIGHT_RIGHTMOST_MBR_PART = 2
    MAX_WEIGHT_GRAM = 3
    WEIGHT_DECAY = 1.5

    def get_mbr_exp_match_tagfile_rank(self, rel_path, mbrParts):
        """
        Object Member Expression File Ranking: Rank higher candiates tags path names that fuzzy match the <expression>.method()
        Rules:
        1) youtube.fetch() --> mbrPaths = ['youtube'] --> get_rank of tag 'fetch' with rel_path a/b/Youtube.js ---> RANK_EXACT_MATCH_RIGHTMOST_MBR_PART_TO_FILENAME
        2) vidtube.fetch() --> tag 'fetch' with rel_path google/video/youtube.js ---> fuzzy match of tri-grams of vidtube (vid,idt,dtu,tub,ube) with tri-grams from the path
        """
        rank = 0
        if len(mbrParts) == 0:
            return rank

        rel_path_no_ext = rel_path.lstrip('.' + os.sep)
        rel_path_no_ext = os.path.splitext(rel_path_no_ext)[0]
        pathParts,dummy_ext = split_rel_path_ext(rel_path)
        print("mbr_exp pathParts %s" % ",".join(pathParts)) # TODO:Debug:Remove
        if len(pathParts) >= 1 and len(
                mbrParts) >= 1 and pathParts[-1].lower() == mbrParts[-1].lower():
            rank += self.RANK_EXACT_MATCH_RIGHTMOST_MBR_PART_TO_FILENAME

        # Prepare dict of <tri-gram : weight>, where weight decays are we move
        # further away from the method call (to the left)
        pathGrams = [get_grams(part) for part in pathParts]
        wt = self.MAX_WEIGHT_GRAM
        dctPathGram = {}
        for setPathGram in reversed(pathGrams):
            dctPathPart = dict.fromkeys(setPathGram, wt)
            dctPathGram = merge_two_dicts_shallow(dctPathPart, dctPathGram)
            wt /= self.WEIGHT_DECAY

        for mbrGrm in self.setMbrGrams:
            rank += dctPathGram.get(mbrGrm, 0)

        return rank

    def concat_import_file_ext_folder_default(self,resolved_path):
        """
        ) If file path without extension - 
          ) If exist and is_folder  - concat /index
          ) concat and test exist in a loop - .[ext1|ext2|...]
        ) else test if exist

        return (final path, True if file exist on disk) 
        """
        final_path = resolved_path
        exist = False
        exts = self.imports.get('file_extensions', [])
        has_ext = len(os.path.splitext(resolved_path)[1]) > 0
        if not has_ext or os.path.isdir(resolved_path):
            file_path = resolved_path 
            if os.path.isdir(resolved_path):
                os.path.join(file_path, self.imports.get('default_folder_file', ''))
            for ext in exts:
                path_with_ext = file_path + "." + ext
                exist = os.path.isfile(path_with_ext)
                if exist:
                    final_path = path_with_ext 
                    break 


        else:
            exist = os.path.isfile(resolved_path)

        return final_path,exist


    def resolve_import_path(self,import_path):
        """
        1) Resolve import path:
           a) Relative path to current file --> '../common/list' --> resolve to abspath
           b) 'common/list' --> try to resolve under node_modules or parent paths
           c) Imports-path env variable name / list of folders
           d) Try file.exist with file extensions: folder (no extension), .js, .jsx, .es6 -- lang setting('extensions') 
              ) extensions are optional in imports - they may exist
           e) If exist resolved path 
              ) If folder --> concat /index.js  
        """
        if len(import_path) == 0:
            print(' error: resolve_import_path: expected non-empty import_path');
            return ''

        rel_re = self.imports.get('is_rel_path', None)
        str_parent_search = self.imports.get('parent_search', None)
        
        resolved_path = import_path
        # starts with '/' - abs path
        if os.path.isabs(import_path) == True:
            resolved_path = import_path
        elif rel_re != None and re.search(rel_re,import_path) != None: # Relative path - ./ or ../ - join it with current file name
            folder_name = os.path.split(self.view.file_name())[0]
            joined_path = os.path.join(folder_name,import_path)
            print('joined_path = %s' % joined_path)
            resolved_path = os.path.realpath(joined_path)   
        
        #TODO: parent search + node_modules concat else:
        #    parent_search = str_parent_search.lower()  == 'true'  

        path_file_exist = self.concat_import_file_ext_folder_default(resolved_path)    

        return path_file_exist

    def prepare_import_rank(self, mbrParts):
        """
        1) Match mbr_exp or function-call to import directive 
        2) Resolve import path:  
        """
        def_path_info = ('',False)
        if hasattr(self, 'import_resolved_path_info'):
            return self.import_resolved_path_info
        imported_symbol = mbrParts[0] if len(mbrParts) > 0 else self.symbol
        self.imports = self.lang.get('imports', {})
        imports_re = self.imports.get('sym_to_import_path', None)
        if imports_re is None:
            return def_path_info
        cur_re = imports_re.replace('__symbol__', imported_symbol)
        print("cur_re=%s" % cur_re)
        # view.find(cur_re) --> Region --> extract Region text --> re.search --> get re group[0] --> imported path
        rgn_imp = self.view.find(cur_re,0)
        str_imp = self.view.substr(rgn_imp)
        print("str_imp=%s" % str_imp)
        m = re.search(cur_re,str_imp)
        if m is None:
            self.import_resolved_path_info = def_path_info
            return self.import_resolved_path_info
        
        imp_path = None
        for i in range(1,len(m.groups()) + 1):
            imp_path = m.group(i)
            if imp_path != None and len(imp_path) > 0:
                break

        print("imp_path=%s" % imp_path)
        
        # Resolve imported path

        final_path, exist = self.resolve_import_path(imp_path)
        self.import_resolved_path_info = (final_path, exist) # store path split into sgements (optional drive+folders+optionally file)  
        
        print("final_path=%s" % final_path ) # TODO:Debug:Remove
        

        return self.import_resolved_path_info

    RANK_IMPORT_FIRST_SEGS_MATCH = 20 # First 2 segments weigh 20 each. 3rd and above - 2 each. 2 first segs match rank 30 - higher than mbr_exp
    RANK_IMPORT_REMAIN_SEGS_MATCH = 2
    RANK_IMPORT_EXT_MATCH = 2

    def get_import_rank(self, rel_path, mbrParts):
        """
        Imported symbols Ranking: const {createServer} = require('https') --> given call to createServer() Rank higher candiates tags path names that match .*/https/index.js 
        Rules:
        1) list.provider.add() --> mbrPaths = ['list','provider'] --> use leftmost mbrPart (or symbol itself if no mbrParts - see createServer above) 
           --> extract path of matching import {list} from 'utils/datastructs'
        2) Resolve import path:           
        3) Symbols from imported file get high rank
            ) Resolved Path matching against tag file partial paths: count matching segments from the right
            ) If this is a file (file_exist - resolved to a file on disk) - require match on right-most segment
              ) else (not sure if file or folder), allow either match of right-most seg or: match second segment + right-most == index.<one of the default exts>   
              ) Ex: import 'utils' --> utils is a  folder -> didn't find utils/index.js --> allow to match also second from the right 
            ) count how many segments match (starting from the right-most/second above) up to max 4 (we do not need to match more)
            ) match ext - bonus point
            ) Q: How to rank tags from imports (only if reliabley found a match) higher than mbr_exp and This (samefile) ?
            ) A: Reliable match: min of 2 matching segs: import {getLogger} from 'utils' --> require 4th/3rd - unmatched/2nd=utils/right-most=index.js 
        """
        rank = 0
        importPath, file_exist = self.prepare_import_rank(mbrParts)
        importPathParts, ext_importPath = split_rel_path_ext(importPath)
        print("importPathParts=%s" % ",".join(importPathParts))
        print("ext_importPath=%s" % ext_importPath) 
        importPathParts.reverse() 
        # 3) Resolved Path matching against tag file partial paths
        # TODO: Rank weights higher than mbr_exp, but not too high ...
        relPathParts, ext_relPath = split_rel_path_ext(rel_path) # TODO:Debug:Remove
        print("relPathParts=%s" % ",".join(relPathParts))
        print("ext_relPath=%s" % ext_relPath)
        relPathParts.reverse()

        idx_imp = 0
        num_match = 0

        for relPart in relPathParts:
            if idx_imp > len(importPathParts) - 1:
                break
            if relPart.lower() != importPathParts[idx_imp].lower():
                if not file_exist and idx_imp == 0 and relPart.lower() == self.imports.get('default_folder_file', ''):
                    num_match += 1
                    continue
                break
                
            idx_imp += 1
            num_match += 1


        if num_match >= 2:
            # num seg matches  + ext match--> rank
            rank += min(2,num_match) * self.RANK_IMPORT_FIRST_SEGS_MATCH
            rank += max(0,num_match - 2) *  self.RANK_IMPORT_REMAIN_SEGS_MATCH
            rank += self.RANK_IMPORT_EXT_MATCH if ext_importPath == ext_relPath else 0 # file extension match - rank higher             
        
        print("import rank=%d" % rank)
        return rank


    def get_combined_rank(self, tag, mbrParts):
        """
        Calculate rank score per tag, combining several heuristics
        """
        rank = 0

        # Type definition Rank
        rank += self.get_type_rank(tag)

        rel_path = tag.tag_path[0]
        # Same file and this.method() ranking
        rank += self.get_samefile_rank(rel_path, mbrParts)

        # Object Member Expression File Ranking
        rank += self.get_mbr_exp_match_tagfile_rank(rel_path, mbrParts)

        # Imported symbols Ranking
        rank += self.get_import_rank(rel_path, mbrParts)

#       print('rank = %d' % rank);
        return rank

    def sort_tags(self, taglist):
        # Scope Filter: If symbol matches at least 1 local scope tag - assume they hides non-scope and global scope tags.
        # If no local-scope (in_scope) matches --> keep the global / no scope matches (see in sorted_tags) and discard
        # the local-scope - because they are not locals of the current position
        # If object-receiver (someobj.symbol) --> refer to as global tag -->
        # filter out local-scope tags
        (in_scope, no_scope) = self.scope_filter(taglist)
        if (len(self.setMbrGrams) == 0 and len(in_scope) >
                0):  # TODO:Config: @symbol - in Ruby instance var (therefore never local var)
            p_tags = in_scope
        else:
            p_tags = no_scope

        p_tags = list(filter(lambda tag: self.pass_def_filter(tag), p_tags))
        for tag in p_tags:
            tag.rank_score = self.get_combined_rank(tag, self.mbrParts)
            
        p_tags = sorted(
            p_tags, key=lambda tag: tag.rank_score , reverse=True)
        return p_tags
