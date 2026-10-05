#!/usr/bin/env bats
# JSON writer.

setup() {
    load ../helpers/common
    ny_lib_setup
}

@test "strings are escaped" {
    run ny_json_str $'a "quoted" \\ back\nnew\ttab'
    assert_output '"a \"quoted\" \\ back\nnew\ttab"'
}

@test "control characters are dropped" {
    run ny_json_str $'bell\a here'
    assert_output '"bell here"'
}

@test "objects mix strings, raw values and nulls" {
    run ny_json_obj name=pi count:=3 ok:=true "missing?=" "present?=x"
    assert_output '{"name":"pi","count":3,"ok":true,"missing":null,"present":"x"}'
    printf '%s' "$output" | jq -e . >/dev/null
}

@test "values containing := or = stay strings" {
    run ny_json_obj "url=http://a:=b=c"
    assert_output '{"url":"http://a:=b=c"}'
}

@test "arrays" {
    run ny_json_arr_str one "two words" 'th"ree'
    assert_output '["one","two words","th\"ree"]'
    run ny_json_arr
    assert_output '[]'
}

@test "bool and num helpers" {
    run ny_json_bool 1
    assert_output true
    run ny_json_bool 0
    assert_output false
    run ny_json_num 12.5
    assert_output 12.5
    run ny_json_num abc
    assert_output null
}
